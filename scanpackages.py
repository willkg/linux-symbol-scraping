#!/usr/bin/env python
"""
The purpose of this script, as discussed at
http://randomascii.wordpress.com/2013/02/20/symbols-on-linux-part-three-linux-versus-windows/,
is to download various Packages files from ddebs.ubuntu.com, download all of the packages listed
within, extract build IDs from files installed by these packages, and add these build IDs to a
single, consolidated, enhanced Packages file -- multiple Packages files, and build IDs,
in one file.
The PackagesProcessed result file contains additional lines of text of the format:
    BuildID SOName PackageURL
The idea is that a simple grep through the file for a build ID will return all of the
information needed to download the relevant package.
"""

from __future__ import print_function

import html5lib
import itertools
import json
import multiprocessing
import os
import re
import requests
import shutil
import subprocess
import sys
import tempfile
import urlparse

from common import fetch_to_file
from concurrent.futures import ThreadPoolExecutor

class AutoSaveDict(dict):
    def __init__(self, path):
        if os.path.isfile(path):
            self.update(json.load(open(path, 'rb')))
        self.path = path
    def __setitem__(self, *args, **kwargs):
        dict.__setitem__(self, *args, **kwargs)
        with tempfile.NamedTemporaryFile(delete=False) as f:
            json.dump(self, f)
            os.rename(f.name, self.path)

def GetBuildID(dso):
    """This function uses 'file' and 'readelf' to see if the specified file is an ELF
file, and if so to try to get its build ID. If no build ID is found then it returns None."""
    # First see if the file is an ELF file -- this avoids error messages
    # from readelf.
    if not subprocess.check_output(['file', '-Lb', dso]).startswith('ELF'):
        return None

    # Now execute readelf. Note that some older versions don't understand build IDs.
    # If you are running such an old version then you can dump the contents of the
    # build ID section and parse the raw data.
    lines = subprocess.check_output(['readelf', '-n', dso]).splitlines()
    # We're looking for this output:
    # Build ID: 99c2106c44189e354e1826aa285a0ccf7cbdf726
    for line in lines:
        match = re.match("Build ID: (.*)", line.strip())
        if match:
            buildID = match.group(1)
            if len(buildID) == 40:
                return buildID
    return None



def FillPackageList(data):
    """This function reads the specified file, which is assumed to be a Packages
file such as those found at http://ddebs.ubuntu.com/dists/precise/main/binary-i386/Packages
and breaks it into individual package description blocks. These blocks are then put into
a dictionary, indexed by the download URL."""
    # Individual package descriptions start with a line that starts with Package: so splitting
    # on this is a simple way to break the file into package blocks.
    packageLabel = "Package: "
    packageCount = 0
    # The URL is the only part of the package that we parse. It is contained in a line
    # that starts with 'Filename: '
    filenameRe = re.compile("Filename: (.*)")
    result = {}
    try:
        for block in data.split("\n" + packageLabel):
            # The splitting process removes the package label from
            # the beginning of all but the first block so let's put it
            # back on.
            if not block.startswith( packageLabel ):
                block = packageLabel + block
            # Look for the package URL
            for line in block.split("\n"):
                line = line.strip()
                match = filenameRe.match(line)
                if match:
                    packageURL = match.groups()[0]
                    # For some reason the Packages file lists some packages multiple times with
                    # the exact same download URL. In every case seen so far the package description
                    # is identical, but lets print a message if that stops being true.
                    if result.has_key(packageURL) and block.strip() != result[packageURL].strip():
                        print("Download URL %s found multiple times with different descriptions." % packageURL)
                    packageCount += 1
                    result[packageURL] = block
    except IOError as e:
        # On the first run the PackagesProcessed file will not exist. We must continue.
        print(e)
    print("Found %d packages" % (packageCount))
    return result


def process_deb(deb_url):
    print('Processing deb %s...' % deb_url)
    buildid_files = []
    try:
        tempDir = tempfile.mkdtemp()
        # Then we download the package.
        deb_file = os.path.join(tempDir, 'file.deb')
        fetch_to_file(deb_url, deb_file)

        # Now we unpack the package
        subprocess.check_call(['dpkg-deb', '-x', deb_file, tempDir])

        for root, dirs, files in os.walk(tempDir):
            for f in files:
                path = os.path.join(root, f)
                buildID = GetBuildID(path)
                if buildID:
                    buildid_files.append(('/' + os.path.relpath(path, tempDir),
                                          buildID))
    finally:
        shutil.rmtree(tempDir)
    return buildid_files

def scan_packages():
    # This is a list of Packages files that we will download and process
    packageTypes = [ "trusty", "trusty-updates" ]
    archs = ["i386", "amd64"]

    startDir = os.getcwd()
    # Iterate through all of the Packages files that we care about.
    for packageType, arch in itertools.product(packageTypes, archs):
        # Download the package list and process it into a dictionary
        # index by the package URLs. The payload is the blob of text
        # associated with the package.
        packageURL = "http://ddebs.ubuntu.com/dists/%s/main/binary-%s/Packages" % (packageType, arch)
        print("Downloading Packages list from %s" % packageURL)
        r = requests.get(packageURL)
        if r.status_code != 200:
                continue
        allPackages = FillPackageList(r.text)
        alreadyProcessed = 0
        processed = 0
        for packageNumber, packageURL in enumerate(allPackages.keys()):
            if processedPackages.has_key(packageURL):
                alreadyProcessed += 1
            else:
                fullPackageURL = 'http://ddebs.ubuntu.com/' + packageURL
                output.write(allPackages[packageURL])
                for path, buildID in process_deb(fullPackageURL):
                    output.write('BuildID: %s %s %s\n' % (buildID, path, fullPackageURL))
                output.write("\n\n")
                print("Processed %d packages, gone through %d of %d." % (processed, packageNumber, len(allPackages)))
                # Put some spacing between separate commands
                print("")
        print("%d were already processed, processed %d more." % (alreadyProcessed, processed))
        print("\n")

    # Make sure buffers are flushed
    output.close()

def scrape_html_directory_listing(url):
    r = requests.get(url)
    if r.status_code == 200:
        doc = html5lib.parse(r.text, treebuilder='dom')
        for a in doc.getElementsByTagName('a'):
            href = a.getAttribute('href')
            text = a.childNodes[0].data
            if href == text:
                yield urlparse.urljoin(url, href)

def scrape_x86_debs(url):
    archs = {'amd64', 'i386'}
    for deb in scrape_html_directory_listing(url):
        arch = os.path.splitext(urlparse.urlparse(deb).path)[0].split('_')[-1]
        if arch in archs:
            yield deb

def scrape_package_list(main_url):
    if os.path.isfile('/tmp/allpackages'):
        return json.load(open('/tmp/allpackages', 'rb'))

    package_list = []
    for url in scrape_html_directory_listing(main_url):
        package_list.extend(list(scrape_html_directory_listing(url)))
        json.dump(package_list, open('/tmp/allpackages', 'wb'))
    return package_list

def chunk(iterable, chunk_size):
    i = iter(iterable)
    while True:
        this_chunk = list(itertools.islice(i, chunk_size))
        if not this_chunk:
            return
        yield this_chunk

def scrape_all_ddebs():
    ddebs = AutoSaveDict('/tmp/ddebs.json')
    processed_packages = AutoSaveDict('/tmp/processed-packages.json')
    with ThreadPoolExecutor(max_workers=multiprocessing.cpu_count()) as executor:
        package_urls = [url for url in scrape_package_list('http://ddebs.ubuntu.com/pool/main/') if url not in processed_packages]
        for urls_chunk in chunk(package_urls, multiprocessing.cpu_count()):
            print('Processing next %d packages...' % len(urls_chunk))
            for url, debs in zip(package_urls,
                                 executor.map(scrape_x86_debs, urls_chunk)):
                print('Processing package %s...' % url)
                debs = [deb for deb in debs if deb not in ddebs]
                print('%d debs to process...' % len(debs))
                for deb, result in zip(debs, executor.map(process_deb, debs)):
                    print('Finished processing deb %s' % deb)
                    ddebs[deb] = result
                processed_packages[url] = True

def main():
    scrape_all_ddebs()

if __name__ == '__main__':
    main()
