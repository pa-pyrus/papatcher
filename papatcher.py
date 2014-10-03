#!/usr/bin/env python3
# vim:fileencoding=utf-8:ts=8:et:sw=4:sts=4:tw=79

"""
papatcher.py: simple python PA patcher

Copyright (c) 2014 Pyrus <pyrus@coffee-break.at>
See the file LICENSE for copying permission.
"""

from argparse import ArgumentParser
from concurrent import futures
from getpass import getpass
from gzip import decompress
from hashlib import sha1
from http.client import OK as HTTP_OK, HTTPSConnection
from json import dumps, loads
from operator import itemgetter
from os import cpu_count, environ
from pathlib import Path
from ssl import create_default_context
from signal import signal, SIGINT
from stat import S_IEXEC
from urllib.error import URLError
from urllib.request import urlopen

import sys

import pycurl

UBERNET_HOST = "uberent.com"
GAME_ROOT = Path(environ["HOME"], ".local", "Uber Entertainment", "PA")
CACHE_DIR = GAME_ROOT / ".cache"
CPU_COUNT = cpu_count()


class ProgressMeter(object):
    def __init__(self):
        self.last_fraction = None

    def display_progress(self, download_total, downloaded,
                         upload_total, uploaded):
        if not int(download_total):
            return
        fraction = (downloaded / download_total) if downloaded else 0

        # display progress only if it has advanced by at least 1 percent
        if self.last_fraction and abs(self.last_fraction - fraction) < 0.01:
            return

        self.last_fraction = fraction

        print("* Progress: {0: >4.0%} of {1} bytes.".format(
            fraction, int(download_total)), end="\r")


class PAPatcher(object):
    """
    PA Patcher class.

    Logs in to UberNet, retrieves stream information and downloads patches.
    """

    def __init__(self, ubername, password, threads, ratelimit):
        """
        Initialize the patcher with UberNet credentials. They will be used to
        login, check for and retrieve patches.
        """
        self.credentials = dumps({"TitleId": 4,
                                  "AuthMethod": "UberCredentials",
                                  "UberName": ubername,
                                  "Password": password})

        ssl_context = create_default_context()
        self.connection = HTTPSConnection(UBERNET_HOST,
                                          context=ssl_context)

        self.threads = threads
        self.ratelimit = ratelimit

    def login(self):
        """
        Login to UberNet and store a session ticket if successful.
        """
        # return immediately if we already have a session ticket
        if hasattr(self, "_session"):
            return True

        # otherwise request a new one
        headers = {"Content-Type": "application/json;charset=utf-8"}
        self.connection.request("POST", "/GC/Authenticate", headers=headers,
                                body=self.credentials)
        response = self.connection.getresponse()
        if response.status is not HTTP_OK:
            print("! Encountered an error: {0} {1}.".format(response.status,
                                                            response.reason))
            return False

        # get and parse response data
        raw_data = response.read()
        result = loads(raw_data.decode("utf-8"))
        if "SessionTicket" not in result:
            print("! Result doesn't contain a session ticket.")
            return False

        self._session = result["SessionTicket"]
        print("* Got Session Ticket: {0}.".format(self._session))
        return True

    def get_streams(self):
        """
        Request and return a list of streams we can download from UberNet.
        """
        # we can't continue without a session ticket
        if not hasattr(self, "_session"):
            return None

        headers = {"X-Authorization": self._session}
        # we no longer need the session ticket
        del self._session

        self.connection.request("GET", "/Launcher/ListStreams?Platform=Linux",
                                headers=headers)
        response = self.connection.getresponse()
        if response.status is not HTTP_OK:
            print("! Encountered an error: {0} {1}.".format(response.status,
                                                            response.reason))
            return None

        # get and parse response data
        raw_data = response.read()
        result = loads(raw_data.decode("utf-8"))
        self._streams = {stream["StreamName"]: stream
                         for stream in result["Streams"]}
        return self._streams

    def get_manifest(self, stream, full):
        if not hasattr(self, "_streams") or stream not in self._streams:
            return False

        self._stream = self._streams[stream]
        # we no longer need all streams
        del self._streams

        print("* Downloading manifest from {0}/{1}/{2}.".format(
            self._stream["DownloadUrl"],
            self._stream["TitleFolder"],
            self._stream["ManifestName"]))

        # we still need to add the AuthSuffix for the download to work
        manifest_url = "{0}/{1}/{2}{3}".format(
            self._stream["DownloadUrl"],
            self._stream["TitleFolder"],
            self._stream["ManifestName"],
            self._stream["AuthSuffix"])

        try:
            with urlopen(manifest_url) as response:
                manifest_raw = decompress(response.read())
                self._manifest = loads(manifest_raw.decode("utf-8"))
                return self._verify_manifest(full)
        except URLError as err:
            print("! Could not retrieve manifest: {0}.".format(err.reason))
            return False

    def _verify_manifest(self, full):
        if not hasattr(self, "_stream") or not hasattr(self, "_manifest"):
            return False

        # clean up cache in the process
        cache_dir = CACHE_DIR / self._stream["StreamName"]
        print("* Verifying contents of cache folder {0}.".format(
            str(cache_dir)))

        if cache_dir.exists():
            bundle_names = [bundle["checksum"]
                            for bundle in self._manifest["bundles"]]

            old_bundles = 0
            for cache_file in cache_dir.iterdir():
                if full or cache_file.name not in bundle_names:
                    cache_file.unlink()
                    old_bundles += 1

            if old_bundles:
                print("* Purged {0} old bundle(s).".format(old_bundles))

        # verify bundles in parallel
        with futures.ThreadPoolExecutor(max_workers=self.threads) as executor:
            # this list will contain the bundles we actually need to download
            self._bundles = list()

            bundle_futures = [executor.submit(self._verify_bundle, bundle)
                              for bundle in self._manifest["bundles"]]

            for completed in futures.as_completed(bundle_futures):
                if not completed.result():
                    # cancel waiting futures
                    for future in bundle_futures:
                        future.cancel()
                    return False

        print("* Need to get {0} bundle(s).".format(len(self._bundles)))

        # if we get here there, all bundles were verified
        # we no longer need the manifest
        del self._manifest
        return True

    def _verify_bundle(self, bundle):
        if not hasattr(self, "_stream") or not hasattr(self, "_bundles"):
            return False

        bundle_checksum = bundle["checksum"]
        cache_file = CACHE_DIR / self._stream["StreamName"] / bundle_checksum

        # if we don't have that file we need to download it
        if not cache_file.exists():
            self._bundles.append(bundle)
            return True

        # if we have it, make sure the checksum is correct
        with cache_file.open("rb") as cache_fp:
            sha = sha1()
            sha.update(cache_fp.read())
            checksum = sha.hexdigest()

            if checksum != bundle_checksum:
                self._bundles.append(bundle)
                return True

        # we have that file and checksums match, nothing to do
        return True

    def patch(self):
        if not hasattr(self, "_bundles"):
            return False

        with futures.ThreadPoolExecutor(max_workers=self.threads) as executor:
            bundle_futures = list()
            # download bundles sorted by size
            self._bundles.sort(key=lambda bundle: int(bundle["size"]),
                               reverse=True)
            for bundle in self._bundles:
                bundle_checksum = bundle["checksum"]

                print("* Downloading bundle {0}.".format(bundle_checksum))
                if not self._download_bundle(bundle):
                    return False

                # bundle was downloaded, start extraction in parallel
                print("* Extracting bundle {0}.".format(bundle_checksum))
                bundle_future = executor.submit(self._extract_bundle, bundle)
                bundle_futures.append(bundle_future)

            for completed in futures.as_completed(bundle_futures):
                if not completed.result():
                    # cancel waiting futures
                    for future in bundle_futures:
                        future.cancel()
                    return False

            # if we're here everything has been downloaded and extracted
            return True

    def _download_bundle(self, bundle):
        if not hasattr(self, "_stream"):
            return False

        bundle_checksum = bundle["checksum"]
        cache_base = CACHE_DIR / self._stream["StreamName"]
        # make sure that path exists
        if not cache_base.exists():
            cache_base.mkdir(parents=True)

        cache_file = cache_base / bundle_checksum

        # remove the file first if it already exists
        if cache_file.exists():
            cache_file.unlink()

        bundle_url = "{0}/{1}/hashed/{2}{3}".format(
            self._stream["DownloadUrl"],
            self._stream["TitleFolder"],
            bundle_checksum,
            self._stream["AuthSuffix"])

        with cache_file.open("x+b") as cache_fp:
            curl = pycurl.Curl()
            curl.setopt(pycurl.URL, bundle_url)
            curl.setopt(pycurl.FOLLOWLOCATION, 1)
            curl.setopt(pycurl.MAXREDIRS, 5)
            curl.setopt(pycurl.CONNECTTIMEOUT, 30)
            curl.setopt(pycurl.NOSIGNAL, 1)
            curl.setopt(pycurl.MAX_RECV_SPEED_LARGE, self.ratelimit)
            curl.setopt(pycurl.WRITEDATA, cache_fp)
            curl.setopt(pycurl.NOPROGRESS, 0)
            progress_meter = ProgressMeter()
            curl.setopt(pycurl.PROGRESSFUNCTION,
                        progress_meter.display_progress)

            try:
                curl.perform()
            except:
                print("! Downloading bundle {0} failed!".format(
                    bundle_checksum))
                return False
            finally:
                curl.close()

            # verify checksum
            cache_fp.seek(0)
            sha = sha1()
            sha.update(cache_fp.read())
            checksum = sha.hexdigest()

            if checksum != bundle_checksum:
                print("! Checksums don't match. Expected {0}, got {1}.".format(
                    bundle_checksum, checksum))
                return False

        # everything worked out OK
        return True

    def _extract_bundle(self, bundle):
        if not hasattr(self, "_stream"):
            return False

        bundle_checksum = bundle["checksum"]
        cache_file = CACHE_DIR / self._stream["StreamName"] / bundle_checksum

        # open cache file with gzip
        with cache_file.open("rb") as cache_fp:
            game_base = GAME_ROOT / self._stream["StreamName"]
            # get entries sorted by offset
            entries = sorted(bundle["entries"], key=itemgetter("offset"))
            for entry in entries:
                entry_file = game_base / entry["filename"][1:]

                # make sure that path exists
                if not entry_file.parent.exists():
                    entry_file.parent.mkdir(parents=True)

                entry_offset = int(entry["offset"])
                cache_fp.seek(entry_offset)

                # remove the file first if it already exists
                if entry_file.exists():
                    entry_file.unlink()

                with entry_file.open("xb") as entry_fp:
                    # data might be compressed further, check sizeZ for that
                    if entry["sizeZ"] != "0":
                        entry_size = int(entry["sizeZ"])
                        raw_data = cache_fp.read(entry_size)
                        entry_fp.write(decompress(raw_data))
                    else:
                        entry_size = int(entry["size"])
                        entry_fp.write(cache_fp.read(entry_size))

                # set executable
                if "executable" in entry:
                    entry_file.chmod(entry_file.stat().st_mode | S_IEXEC)

        return True


if __name__ == "__main__":
    print("Python PA Patcher\n"
          "=================")

    signal(SIGINT, lambda sig, frame: sys.exit(SIGINT))

    arg_parser = ArgumentParser()
    arg_parser.add_argument("-u", "--ubername",
                            action="store", type=str,
                            help="UberName used for login.")
    arg_parser.add_argument("-p", "--password",
                            action="store", type=str,
                            help="Password used for login.")
    arg_parser.add_argument("-s", "--stream",
                            action="store", type=str,
                            help="Stream being downloaded.")
    arg_parser.add_argument("-f", "--full",
                            action="store_true",
                            help="Patch even unchanged files.")
    arg_parser.add_argument("-t", "--threads",
                            action="store", type=int,
                            default=CPU_COUNT,
                            help="Number of threads used.")
    arg_parser.add_argument("-r", "--ratelimit",
                            action="store", type=int,
                            default=0,
                            help="Limit downloads to bytes/sec.")
    arg_parser.add_argument("--unattended",
                            action="store_true",
                            help="Don't ask any questions. If you use this "
                                 "option, --ubername, --password and --stream "
                                 "are mandatory")

    arguments = arg_parser.parse_args()
    unattended = arguments.unattended
    if (unattended and not (arguments.ubername and
                            arguments.password and
                            arguments.stream)):
        print("! For unattended mode you need to use "
              "--ubername, --password and --stream. "
              "Exiting...")
        sys.exit(-1)

    ubername = arguments.ubername or input("? UberName: ")
    password = arguments.password or getpass("? Password: ")

    print("* Creating patcher...")
    patcher = PAPatcher(ubername, password,
                        arguments.threads, arguments.ratelimit)

    print("* Logging in to UberNet...")
    if not patcher.login():
        print("! Login failed. Exiting...")
        sys.exit(-1)

    print("* Requesting streams...")
    streams = patcher.get_streams()
    if not streams:
        print("! Could not acquire streams. Exiting...")
        sys.exit(-1)

    stream = arguments.stream
    if not stream or stream not in streams:
        if unattended:
            print("! Invalid Stream. "
                  "For a selection of streams use interactive mode. "
                  "Exiting...")
            sys.exit(-1)

        while True:
            print("* Available streams: {0}.".format(
                ", ".join(streams.keys())))

            stream = input("? Select stream: ")
            if stream in streams:
                break
            print("! Invalid Stream.")

    print("* Downloading manifest for stream '{0}'...".format(stream))
    if not patcher.get_manifest(stream, arguments.full):
        print("! Could not download manifest. Exiting...")
        sys.exit(-1)

    print("* Patching installation for stream '{0}'...".format(stream))
    if not patcher.patch():
        print("! Could not patch stream. Exiting...")
        sys.exit(-1)

    print("* Successfully updated stream '{0}'.".format(stream))
    sys.exit(0)
