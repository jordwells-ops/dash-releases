#!/usr/bin/env python3
"""Promote explicitly approved non-touch firmware; never execute release assets."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import struct
import subprocess
import time
from urllib.parse import quote
from urllib.request import Request, urlopen

REPO = 'jordwells-ops/dash-releases'
HARDWARE = 'esp32-s3-lcd-2.8c-nontouch'
PREFIX = 'firmware-nontouch-v'
VERSION = re.compile(r'(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-(alpha|pilot|beta|rc)\.(0|[1-9]\d*))?')
MAX_IMAGE = 3 * 1024 * 1024


def version_key(value):
    match = VERSION.fullmatch(value) if isinstance(value, str) else None
    if not match:
        raise ValueError('Use a release version, not a development/build version')
    major, minor, patch, channel, iteration = match.groups()
    return (int(major), int(minor), int(patch),
            {None: 4, 'rc': 3, 'beta': 2, 'pilot': 1, 'alpha': 0}[channel],
            int(iteration or 0))


def image_version(image):
    if len(image) < 256 or len(image) > MAX_IMAGE or image[0] != 0xE9:
        raise ValueError('Not an OTA-sized ESP application image')
    if struct.unpack_from('<H', image, 12)[0] != 9:
        raise ValueError('Firmware must target ESP32-S3')
    if struct.unpack_from('<I', image, 32)[0] != 0xABCD5432:
        raise ValueError('Missing ESP application descriptor')
    return image[48:80].split(b'\0', 1)[0].decode('ascii')


def approved_metadata(image, version, notes):
    version_key(version)
    if image_version(image) != version:
        raise ValueError('Filename/version does not match the compiled firmware')
    return dict(schema=1, approved=True, hardware=HARDWARE, version=version,
                filename=f'dash-nontouch-{version}.bin', size=len(image),
                sha256=hashlib.sha256(image).hexdigest(), notes=notes[:2000])


def asset(release, name):
    matches = [a for a in release.get('assets', []) if a.get('name') == name]
    if len(matches) != 1 or matches[0].get('state') != 'uploaded':
        raise ValueError('Release needs exactly one uploaded ' + name)
    result = matches[0]
    expected = f'https://github.com/{REPO}/releases/download/{quote(release["tag_name"], safe="")}/{quote(name, safe="")}'
    if result.get('browser_download_url') != expected:
        raise ValueError('Asset URL is outside this exact release')
    return result


def validate(release, metadata, image, current):
    if release.get('draft') or not release.get('published_at'):
        raise ValueError('Only published releases can be promoted')
    version = metadata.get('version')
    version_key(version)
    if release.get('tag_name') != PREFIX + version:
        raise ValueError('Not a matching non-touch firmware release tag')
    if bool(release.get('prerelease')) != ('-' in version):
        raise ValueError('GitHub beta/stable status disagrees with the version')
    if metadata.get('schema') != 1 or metadata.get('approved') is not True:
        raise ValueError('Firmware release is not explicitly approved')
    if metadata.get('hardware') != HARDWARE:
        raise ValueError('Wrong display hardware; do not mix touch/non-touch images')
    expected = approved_metadata(image, version, str(metadata.get('notes', '')))
    for key in ('filename', 'size', 'sha256'):
        if metadata.get(key) != expected[key]:
            raise ValueError('Firmware metadata mismatch: ' + key)
    if asset(release, expected['filename']).get('size') != len(image):
        raise ValueError('Asset size does not match downloaded image')
    if current:
        if current.get('hardware') != HARDWARE:
            raise ValueError('Existing feed has a different hardware target')
        old, new = version_key(current['version']), version_key(version)
        if new < old:
            raise ValueError('Refusing to downgrade the approved firmware feed')
        if new == old and metadata['sha256'] != current.get('sha256'):
            raise ValueError('An approved version cannot be replaced with different bytes')
    return expected


def download(url, limit):
    # Public release/raw downloads deliberately receive NO GitHub credential.
    with urlopen(Request(url, headers={'User-Agent': 'DASH-approved-firmware'}), timeout=45) as response:
        data = response.read(limit + 1)
    if len(data) > limit:
        raise ValueError('Download exceeds its size limit')
    return data


def git(*args):
    return subprocess.check_output(['git', *args], text=True).strip()


def commit_push(paths, message):
    git('add', '--', *paths)
    if not git('diff', '--cached', '--name-only'):
        return git('rev-parse', 'HEAD')
    git('-c', 'user.name=DASH release bot', '-c',
        'user.email=41898282+github-actions[bot]@users.noreply.github.com',
        'commit', '-m', message)
    git('push', 'origin', 'HEAD:main')
    return git('rev-parse', 'HEAD')


def promote(tag, dry_run=False):
    if not tag.startswith(PREFIX):
        raise ValueError('Windows, other hardware and unrelated releases are not firmware')
    version_key(tag[len(PREFIX):])
    if os.environ.get('GITHUB_REPOSITORY', REPO) != REPO:
        raise ValueError('Unexpected release repository')
    request = Request(f'https://api.github.com/repos/{REPO}/releases/tags/{quote(tag, safe="")}',
                      headers={'Accept': 'application/vnd.github+json',
                               'Authorization': 'Bearer ' + os.environ['GITHUB_TOKEN'],
                               'User-Agent': 'DASH-approved-firmware'})
    with urlopen(request, timeout=30) as response:
        release = json.load(response)
    meta_asset = asset(release, 'firmware-release.json')
    metadata = json.loads(download(meta_asset['browser_download_url'], 16384))
    version = metadata.get('version')
    version_key(version)
    filename = f'dash-nontouch-{version}.bin'
    binary_asset = asset(release, filename)
    image = download(binary_asset['browser_download_url'], MAX_IMAGE)
    manifest_path = Path('manifest.json')
    current = json.loads(manifest_path.read_text()) if manifest_path.exists() else None
    expected = validate(release, metadata, image, current)
    if current and current['version'] == version:
        print('Already the approved version; feed unchanged.')
        return
    if dry_run:
        print('PASS: approved firmware validated; feed unchanged.')
        return
    if git('status', '--porcelain'):
        raise ValueError('Refusing to publish from a dirty checkout')
    target = Path(filename)
    if target.exists() and target.read_bytes() != image:
        raise ValueError('Refusing to overwrite an existing versioned image')
    target.write_bytes(image)
    image_commit = commit_push([filename], 'Stage approved firmware ' + version)
    # Immutable URL + a separate final manifest commit: the image is reachable
    # before any existing device or website sees a new update announcement.
    url = f'https://raw.githubusercontent.com/{REPO}/{image_commit}/{filename}'
    for attempt in range(6):
        try:
            public = download(url, MAX_IMAGE)
            if hashlib.sha256(public).hexdigest() != expected['sha256']:
                raise ValueError('Public image checksum mismatch')
            break
        except Exception:
            if attempt == 5:
                raise
            time.sleep(2 * (attempt + 1))
    manifest = {k: expected[k] for k in ('version', 'hardware', 'sha256', 'notes', 'size')}
    manifest.update(url=url, approved=True, release_tag=tag,
                    release_url=f'https://github.com/{REPO}/releases/tag/{quote(tag, safe="")}',
                    approved_at=release['published_at'])
    manifest_path.write_text(json.dumps(manifest, indent=2) + '\n')
    commit_push(['manifest.json'], 'Promote approved firmware ' + version)
    print('Published approved firmware manifest for ' + version)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tag', required=True)
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    promote(args.tag, args.dry_run)
