#!/usr/bin/env python
'''
Given a ddebs.json file generated by scanpackages.py, fetch the most
recent missing symbols list from crash-analysis.mozilla.com and dump symbols
from any files whose debs can be found.
'''

from __future__ import print_function

import concurrent.futures
import datetime
import itertools
import json
import optparse
import os
import requests
import shutil
import subprocess
import sys
import tempfile
import urllib
import urlparse
import zipfile

from collections import defaultdict
from threading import Lock
from concurrent.futures import ThreadPoolExecutor

from common import fetch_to_file

print_lock = Lock()
p = print

def print(*a, **b):
    with print_lock:
        p(*a, **b)

SYMBOL_SERVER_URL = 'https://s3-us-west-2.amazonaws.com/org.mozilla.crash-stats.symbols-public/v1/'
MISSING_SYMBOLS_URL = 'https://crash-analysis.mozilla.com/crash_analysis/{date}/{date}-missing-symbols.txt'

def server_has_file(filename):
    '''
    Send the symbol server a HEAD request to see if it has this symbol file.
    '''
    r = requests.head(urlparse.urljoin(SYMBOL_SERVER_URL, urllib.quote(filename)))
    return r.status_code == 200

def just_linux_symbols(file):
    symbols = set()
    lines = iter(file.splitlines())
    # Skip header
    next(lines)
    for line in lines:
        line = unicode(line.rstrip(), 'utf-8').encode('ascii', 'replace')
        bits = line.split(',')
        if len(bits) < 2:
            continue
        debug_file, debug_id = bits[:2]
        if debug_file.endswith('.so'):
            symbols.add((debug_file, debug_id))
    return symbols

def munge_build_id(build_id):
    '''
    Breakpad stuffs the build id into a GUID struct so the bytes are
    flipped from the standard presentation.
    '''
    b = map(''.join, zip(*[iter(build_id.upper())]*2))
    return ''.join(itertools.chain(reversed(b[:4]), reversed(b[4:6]),
                                   reversed(b[6:8]), b[8:16])) + '0'

def fetch_missing_symbols(verbose):
    now = datetime.datetime.now()
    for n in range(5):
        d = now + datetime.timedelta(days=-n)
        u = MISSING_SYMBOLS_URL.format(date=d.strftime('%Y%m%d'))
        cached_path = os.path.join('/tmp',
                                   os.path.basename(urlparse.urlparse(u).path))
        content = None
        if os.path.isfile(cached_path):
            content = open(cached_path, 'rb').read()
        else:
            r = requests.get(u)
            if r.status_code == 200:
                if verbose:
                    print('Fetching missing symbols from %s' % u)
                content = r.content
                open(cached_path, 'wb').write(content)
        if content:
            return just_linux_symbols(content)
    return set()

def fetch_missing_symbols_from_crash(verbose, crash_id):
    url = 'https://crash-stats.mozilla.com/api/ProcessedCrash/?crash_id={crash_id}&datatype=processed'.format(crash_id = crash_id)
    if verbose:
        print('Fetching missing symbols from crash: %s' % url)
    r = requests.get(url)
    if r.status_code != 200:
        return set()
    j = r.json()
    return set([(m['debug_file'], m['debug_id']) for m in j['json_dump']['modules'] if 'missing_symbols' in m])

def make_build_id_map(ddebs_file):
    id_map = {}
    cache_file = '/tmp/packages.json'
    if os.path.exists(cache_file) and os.stat(cache_file).st_mtime > os.stat(ddebs_file).st_mtime:
        return json.load(open(cache_file, 'rb'))
    ddebs = json.load(open(ddebs_file, 'rb'))
    for package, data in ddebs.iteritems():
        for filename, build_id in data:
            id_map[munge_build_id(build_id)] = (filename, package)
    with open(cache_file, 'wb') as f:
        json.dump(id_map, f)
    return id_map

def make_sym_filename(filename, debug_id):
    f = os.path.basename(filename)
    return os.path.join(f, debug_id, f + '.sym')

def process_deb(verbose, dump_syms, deb_url, files):
    files = [(f, s) for (f, s) in files if not server_has_file(s)]
    if not files:
        # We must have all these symbols already.
        if verbose:
            print('No files to process from %s' % deb_url)
        return []
    if verbose:
        print('Processing %d files from %s' % (len(files), deb_url))
    try:
        tmpdir = tempfile.mkdtemp(suffix='.scrapedebs')
        deb_file = os.path.join(tmpdir, 'file.deb')
        fetch_to_file(deb_url, deb_file)
        # Extract out just the files we want
        subprocess.check_call('dpkg-deb --fsys-tarfile {deb} | tar x {files}'.format(deb=deb_file, files=' '.join('.' + f for f, s in files)),
                              cwd=tmpdir,
                              shell=True)
        symbols = []
        for filename, symbol_file in files:
            path = os.path.join(tmpdir, filename[1:])
            if verbose:
                print('Processing %s' % filename)
            symbols.append((symbol_file,
                            subprocess.check_output([dump_syms, path])))
        return symbols
    except subprocess.CalledProcessError:
        return []
    finally:
        shutil.rmtree(tmpdir)

def main():
    parser = optparse.OptionParser()
    parser.add_option('-v', '--verbose', dest='verbose', action='store_true')
    parser.add_option('--ddebs', dest='ddebs', action='store')
    parser.add_option('--dump-syms', dest='dump_syms', action='store')
    parser.add_option('--from-crash', dest='from_crash', action='store')
    options, args = parser.parse_args()
    # Fetch list of missing symbols
    if options.from_crash:
        missing_symbols = fetch_missing_symbols_from_crash(options.verbose,
                                                           options.from_crash)
    else:
        missing_symbols = fetch_missing_symbols(options.verbose)
    if options.verbose:
        print('Found %d missing symbols' % len(missing_symbols))
    # Fetch list of known Build IDs
    if options.ddebs:
        build_id_map = make_build_id_map(options.ddebs)
        if options.verbose:
            print('Found %d known build IDs' % len(build_id_map))
    else:
        build_id_map = {}
    # Group files by source deb.
    debs_to_process = defaultdict(list)
    for f, debug_id in missing_symbols:
        res = build_id_map.get(debug_id, None)
        if res is not None:
            filename, ddeb = res
            sym_filename = make_sym_filename(filename, debug_id)
            debs_to_process[ddeb].append((filename, sym_filename))
    if options.verbose:
        print('%d ddebs to process (%d files)' % (len(debs_to_process), sum(len(f) for f in debs_to_process.itervalues())))
    # Now fetch each deb and dump symbols from the files within.
    file_list = []
    with zipfile.ZipFile('symbols.zip', 'w', zipfile.ZIP_DEFLATED) as zf, ThreadPoolExecutor(max_workers=4) as executor:
        for result in executor.map(lambda x : process_deb(options.verbose, options.dump_syms, *x), debs_to_process.iteritems()):
            for symbol_file, contents in result:
                if symbol_file and contents:
                    file_list.append(symbol_file)
                    zf.writestr(symbol_file, contents)
        # Add an index file.
        zf.writestr('ubuntusyms-1.0-Linux-{date}-symbols.txt'.format(date=datetime.datetime.now().strftime('%Y%m%d%H%M%S')),
                    '\n'.join(file_list) + '\n')
    if file_list:
        if options.verbose:
            print('Generated symbols.zip with %d symbols' % len(file_list))
    else:
        if options.verbose:
            print('No symbols found.')
        os.unlink('symbols.zip')

if __name__ == '__main__':
    main()
