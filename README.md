# papatcher #
A simple patcher for Planetary Annihilation. It is meant for use on Linux systems.

It takes your Uber credentials and downloads the newest build for the stream of your choice.
The game is installed in `~/.local/Uber Entertainment/PA/<StreamName>`.

The patcher creates a cache of downloaded files in `~/.local/Uber Entertainment/PA/.cache/<StreamName>`.

## Installation ##
papatcher relies on a working python3 (at least v3.2) environment.

## Usage ##
1. Run `./papatcher.py`
2. Enter your UberName and Password
3. Select the stream you want to use. (Usually `stable`)
4. Wait for the process to complete.
5. (Optional) Delete the files from the download cache.
   If you do this, the patcher will download them again next time it is run.

## License ##
Copyright (c) 2014 Pyrus <pyrus@coffee-break.at>  
See the file LICENSE for copying permission.
