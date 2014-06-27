# papatcher #
A simple patcher for Planetary Annihilation. It is meant for use on Linux systems.

It takes your Uber credentials and downloads the newest build for the stream of your choice.
The game is installed in `~/.local/Uber Entertainment/PA/<StreamName>`.

The patcher creates a cache of downloaded files in `~/.local/Uber Entertainment/PA/.cache/<StreamName>`.

## Installation ##
papatcher relies on a working python3 (at least v3.4) environment.

## Usage ##
The patcher supports both interactive and unattended operation.
Entering credentials and stream selection can be achieved using command line arguments.
The patcher will query missing information if it's run in interactive mode.
In unattended mode, missing information will cause the patcher to terminate.

Start the patcher with `-h` or `--help` for a full list of command line arguments.

### Interactive ###
1. Run `./papatcher.py`
2. Enter your UberName and Password
3. Select the stream you want to use. (Usually `stable`)
4. Wait for the process to complete.
5. (Optional) Delete the files from the download cache.
   If you do this, the patcher will download them again next time it is run.

### Unattended ###
1. Run `./papatcher.py -u <UberName> -p <Password> -s <Stream> --unattended`
2. Wait for the process to complete.
3. (Optional) Delete the files from the download cache.
   If you do this, the patcher will download them again next time it is run.

## Acknowledgements ##
This patcher is basically a re-implementation of the [Go PA patcher](https://bitbucket.org/papatcher/papatcher) by Uber Entertainment's very own *William Howe-Lott*.

## License ##
Copyright (c) 2014 Pyrus <pyrus@coffee-break.at>  
See the file LICENSE for copying permission.
