#!/usr/bin/env python3
# vim:fileencoding=utf-8:ts=8:et:sw=4:sts=4:tw=79

"""
papatcher.py: simple python PA patcher

Copyright (c) 2014 Pyrus <pyrus@coffee-break.at>
See the file LICENSE for copying permission.
"""

from concurrent import futures
from getpass import getpass
from gzip import decompress
from hashlib import sha1
from http.client import OK as HTTP_OK
from json import dumps, loads
from operator import itemgetter
from os import chmod, makedirs, stat, listdir, remove
from signal import signal, SIGINT
from stat import S_IEXEC
from urllib.error import URLError
from urllib.request import urlopen

import sys
import os.path

try:
    import ssl
except ImportError:
    from http.client import HTTPConnection
else:
    from http.client import HTTPSConnection

UBERNET_HOST = "uberent.com"
GAME_ROOT = os.path.expanduser(os.path.join("~", ".local",
                                            "Uber Entertainment",
                                            "PA"))
CACHE_DIR = os.path.join(GAME_ROOT, ".cache")


class PAPatcher(object):
    """
    PA Patcher class.

    Logs in to UberNet, retrieves stream information and downloads patches.
    """

    def __init__(self, ubername, password):
        """
        Initialize the patcher with UberNet credentials. They will be used to
        login, check for and retrieve patches.
        """
        self.credentials = dumps({"TitleId": 4,
                                  "AuthMethod": "UberCredentials",
                                  "UberName": ubername,
                                  "Password": password})

        if "ssl" in globals():
            ssl_context = ssl.SSLContext(ssl.PROTOCOL_SSLv3)
            self.connection = HTTPSConnection(UBERNET_HOST,
                                              context=ssl_context)
        else:
            self.connection = HTTPConnection(UBERNET_HOST)

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

    def get_manifest(self, stream):
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
            response = urlopen(manifest_url)
            manifest_raw = decompress(response.read())
            self._manifest = loads(manifest_raw.decode("utf-8"))
            return self._verify_manifest()
        except URLError as err:
            print("! Could not retrieve manifest: {0}.".format(err.reason))
            return False

    def _verify_manifest(self):
        if not hasattr(self, "_stream") or not hasattr(self, "_manifest"):
            return False

        # clean up cache in the process
        cache_dir = os.path.join(CACHE_DIR, self._stream["StreamName"])
        if os.path.exists(cache_dir):
            cache_files = listdir(cache_dir)

            bundle_names = [bundle["checksum"]
                            for bundle in self._manifest["bundles"]]

            old_bundles = 0
            for cache_file_name in cache_files:
                if cache_file_name not in bundle_names:
                    cache_file = os.path.join(cache_dir, cache_file_name)
                    remove(cache_file)
                    old_bundles += 1

            if old_bundles:
                print("* Purged {0} old bundles.".format(old_bundles))

        # verify bundles in parallel
        with futures.ThreadPoolExecutor(max_workers=4) as executor:
            # this list will contain the bundles we actually need to download
            self._bundles = list()

            bundle_futures = [executor.submit(self._verify_bundle, bundle)
                              for bundle in self._manifest["bundles"]]

            for future in futures.as_completed(bundle_futures):
                if not future.result():
                    executor.shutdown(wait=False)
                    return False

            print("* Need to get {0} bundles.".format(len(self._bundles)))

            # if we get here there, all bundles were verified
            # we no longer need the manifest
            del self._manifest
            return True

    def _verify_bundle(self, bundle):
        if not hasattr(self, "_stream") or not hasattr(self, "_bundles"):
            return False

        bundle_checksum = bundle["checksum"]

        cache_file_path = os.path.join(CACHE_DIR,
                                       self._stream["StreamName"],
                                       bundle_checksum)

        # if we don't have that file we need to download it
        if not os.path.exists(cache_file_path):
            self._bundles.append(bundle)
            return True

        # if we have it, make sure the checksum is correct
        with open(cache_file_path, "rb") as cache_file:
            sha = sha1()
            sha.update(cache_file.read())
            checksum = sha.hexdigest()

            if checksum != bundle_checksum:
                self._bundles.append(bundle)
                return True

        # we have that file and checksums match, nothing to do
        return True

    def patch(self):
        if not hasattr(self, "_bundles"):
            return False

        with futures.ThreadPoolExecutor(max_workers=16) as executor:
            bundle_futures = [executor.submit(self._download_bundle, bundle)
                              for bundle in self._bundles]
            for future in futures.as_completed(bundle_futures):
                if not future.result():
                    executor.shutdown(wait=False)
                    return False

            # if we're here everything has been downloaded and extracted
            return True

    def _download_bundle(self, bundle):
        if not hasattr(self, "_stream"):
            return False

        bundle_checksum = bundle["checksum"]
        cache_file_path = os.path.join(CACHE_DIR,
                                       self._stream["StreamName"],
                                       bundle_checksum)

        # make sure that path exists
        base_dir = os.path.dirname(cache_file_path)
        makedirs(base_dir, exist_ok=True)

        bundle_url = "{0}/{1}/hashed/{2}{3}".format(
            self._stream["DownloadUrl"],
            self._stream["TitleFolder"],
            bundle_checksum,
            self._stream["AuthSuffix"])

        try:
            response = urlopen(bundle_url)
        except URLError as err:
            print("! Downloading bundle {0} failed: {0}.".format(
                bundle_checksum, err.reason))
            return False

        with open(cache_file_path, "w+b") as cache_file:
            cache_file.write(response.read())
            cache_file.flush()

            # verify checksum
            cache_file.seek(0)
            sha = sha1()
            sha.update(cache_file.read())
            checksum = sha.hexdigest()

            if checksum != bundle_checksum:
                print("! Checksums don't match. Expected {0}, got {1}.".format(
                    bundle_checksum, checksum))
                return False

        return self._extract_bundle(bundle)

    def _extract_bundle(self, bundle):
        if not hasattr(self, "_stream"):
            return False

        bundle_checksum = bundle["checksum"]
        cache_file_path = os.path.join(CACHE_DIR,
                                       self._stream["StreamName"],
                                       bundle_checksum)

        # open cache file with gzip
        with open(cache_file_path, "rb") as cache_file:
            # get entries sorted by offset
            entries = sorted(bundle["entries"], key=itemgetter("offset"))
            for entry in entries:
                entry_path = os.path.join(GAME_ROOT,
                                          self._stream["StreamName"],
                                          entry["filename"][1:])
                print("* Extracting {0}".format(entry_path))

                # make sure that path exists
                base_dir = os.path.dirname(entry_path)
                makedirs(base_dir, exist_ok=True)

                entry_offset = int(entry["offset"])
                cache_file.seek(entry_offset)

                entry_file = open(entry_path, "w+b")

                # data might be compressed further, we know if there is sizeZ
                if entry["sizeZ"] != "0":
                    entry_size = int(entry["sizeZ"])
                    raw_data = cache_file.read(entry_size)
                    entry_file.write(decompress(raw_data))
                else:
                    entry_size = int(entry["size"])
                    entry_file.write(cache_file.read(entry_size))

                entry_file.close()

                # set executable
                if "executable" in entry:
                    st = stat(entry_path)
                    chmod(entry_path, st.st_mode | S_IEXEC)

        return True


if __name__ == "__main__":
    print("Python PA Patcher\n"
          "=================")

    signal(SIGINT, lambda sig, frame: sys.exit(SIGINT))

    if "ssl" not in globals():
        while True:
            print("! SSL is not supported. "
                  "Login to Ubernet will NOT be encrypted!")
            cont = input("? Continue [yes|no]: ")

            if "no" == cont.lower():
                print("! Exiting...")
                sys.exit(-1)
            elif "yes" == cont.lower():
                print("* Proceeding without encryption.")
                break

            print("! Please type 'yes' or 'no'.")

    ubername = input("? UberName: ")
    password = getpass("? Password: ")

    print("* Creating patcher...")
    patcher = PAPatcher(ubername, password)

    print("* Logging in to UberNet...")
    if not patcher.login():
        print("! Login failed. Exiting...")
        sys.exit(-1)

    print("* Requesting streams...")
    streams = patcher.get_streams()
    if not streams:
        print("! Could not acquire streams. Exiting...")
        sys.exit(-1)

    while True:
        print("* Available streams: {0}.".format(", ".join(streams.keys())))
        stream = input("? Select stream: ")
        if stream in streams:
            break
        print("! Invalid Stream.")

    print("* Downloading manifest for stream '{0}'...".format(stream))
    if not patcher.get_manifest(stream):
        print("! Could not download manifest. Exiting...")
        sys.exit(-1)

    print("* Patching installation for stream '{0}'...".format(stream))
    if not patcher.patch():
        print("! Could not patch stream. Exiting...")
        sys.exit(-1)

    print("* Successfully updated stream '{0}'.".format(stream))
    sys.exit(0)
