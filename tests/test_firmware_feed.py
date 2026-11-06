import copy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import struct
import subprocess
import tempfile
import unittest
from unittest.mock import patch

path = Path(__file__).resolve().parents[1] / 'scripts/firmware_feed.py'
spec = importlib.util.spec_from_file_location('firmware_feed', path)
feed = importlib.util.module_from_spec(spec)
spec.loader.exec_module(feed)


def fixture(version='0.1.0-beta.12'):
    image = bytearray(512)
    image[0] = 0xE9
    struct.pack_into('<H', image, 12, 9)
    struct.pack_into('<I', image, 32, 0xABCD5432)
    image[48:48+len(version)] = version.encode()
    image = bytes(image)
    metadata = feed.approved_metadata(image, version, 'Tested release')
    tag = feed.PREFIX + version
    def asset(name, size):
        return dict(name=name, state='uploaded', size=size,
            browser_download_url=f'https://github.com/{feed.REPO}/releases/download/{tag}/{name}')
    release = dict(tag_name=tag, draft=False, prerelease='-' in version,
        published_at='2026-11-05T12:00:00Z', assets=[
            asset('firmware-release.json', 400), asset(metadata['filename'], len(image))])
    current = dict(version='0.1.0-beta.6', hardware=feed.HARDWARE, sha256='0'*64)
    return release, metadata, image, current


class FirmwareFeedTests(unittest.TestCase):
    def test_approved_beta_and_stable(self):
        for version in ('0.1.0-beta.12', '0.1.0'):
            args = fixture(version)
            self.assertEqual(feed.validate(*args)['version'], version)

    def test_numerical_version_order_and_dev_rejection(self):
        self.assertGreater(feed.version_key('0.1.0-beta.12'), feed.version_key('0.1.0-beta.9'))
        self.assertGreater(feed.version_key('0.1.0'), feed.version_key('0.1.0-rc.2'))
        for invalid in ('0.1.0-beta.11-dev.5', '0.1.0-dev', '../evil', '1.0', '01.0.0'):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                feed.version_key(invalid)

    def test_draft_unapproved_wrong_hardware_and_windows_rejected(self):
        for field, value in [('draft', True), ('published_at', None),
                             ('tag_name', 'windows-v0.3.5-beta'), ('prerelease', False)]:
            release, metadata, image, current = fixture()
            release[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                feed.validate(release, metadata, image, current)
        for field, value in [('approved', False), ('approved', 'true'),
                             ('schema', 2), ('hardware', 'touch')]:
            release, metadata, image, current = fixture()
            metadata[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                feed.validate(release, metadata, image, current)

    def test_hash_size_filename_and_embedded_version_must_match(self):
        for field, value in [('sha256', '1'*64), ('size', 10),
                             ('filename', 'other.bin'), ('version', '0.1.0-beta.13')]:
            release, metadata, image, current = fixture()
            metadata[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                feed.validate(release, metadata, image, current)
        args = fixture()
        with self.assertRaises(ValueError):
            feed.validate(args[0], args[1], args[2][:-1]+b'x', args[3])

    def test_image_chip_and_descriptor_validation(self):
        for offset in (0, 12, 32, 48):
            args = list(fixture())
            image = bytearray(args[2]); image[offset] ^= 1
            with self.subTest(offset=offset), self.assertRaises(ValueError):
                feed.approved_metadata(bytes(image), args[1]['version'], '')

    def test_missing_duplicate_unuploaded_and_external_assets_rejected(self):
        for change in ('missing', 'duplicate', 'uploading', 'external'):
            release, metadata, image, current = fixture()
            if change == 'missing': release['assets'].pop()
            elif change == 'duplicate': release['assets'].append(copy.deepcopy(release['assets'][1]))
            elif change == 'uploading': release['assets'][1]['state'] = 'new'
            else: release['assets'][1]['browser_download_url'] = 'https://evil.example/image.bin'
            with self.subTest(change=change), self.assertRaises(ValueError):
                feed.validate(release, metadata, image, current)

    def test_downgrade_and_same_version_replacement_rejected(self):
        release, metadata, image, current = fixture()
        current['version'] = '1.0.0'
        with self.assertRaises(ValueError): feed.validate(release, metadata, image, current)
        current['version'] = metadata['version']
        with self.assertRaises(ValueError): feed.validate(release, metadata, image, current)
        current['sha256'] = hashlib.sha256(image).hexdigest()
        feed.validate(release, metadata, image, current)

    def test_code_publishes_image_before_manifest_with_immutable_url(self):
        source = path.read_text()
        self.assertLess(source.index('image_commit = commit_push'), source.index("manifest_path.write_text"))
        self.assertIn('{image_commit}/{filename}', source)
        self.assertIn("if dry_run:", source)

    def test_real_git_promotion_is_ordered_repeatable_and_dry_run_safe(self):
        release, metadata, image, current = fixture()
        previous = Path.cwd()
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            origin, checkout = base/'origin.git', base/'checkout'
            def run(*args):
                return subprocess.check_output(['git', *args], stderr=subprocess.DEVNULL, text=True).strip()
            run('init', '--bare', '--initial-branch=main', str(origin))
            run('clone', str(origin), str(checkout))
            try:
                os.chdir(checkout)
                Path('manifest.json').write_text(json.dumps(current))
                run('add', 'manifest.json')
                run('-c','user.name=Test','-c','user.email=test@example.invalid','commit','-m','Initial')
                run('push','origin','main')
                initial = run('rev-parse','HEAD')
                def download(url, limit):
                    if url.endswith('/firmware-release.json'): return json.dumps(metadata).encode()
                    if 'raw.githubusercontent.com' in url:
                        # First push must already contain the image, while
                        # remote manifest must STILL be the old approved feed.
                        self.assertEqual(json.loads(run('--git-dir='+str(origin),'show','main:manifest.json'))['version'],current['version'])
                        self.assertEqual(run('--git-dir='+str(origin),'rev-parse','main'),url.split('/')[-2])
                    return image
                with patch.dict(os.environ,{'GITHUB_TOKEN':'unit-test-only','GITHUB_REPOSITORY':feed.REPO}), \
                     patch.object(feed,'urlopen',return_value=io.BytesIO(json.dumps(release).encode())), \
                     patch.object(feed,'download',side_effect=download):
                    feed.promote(release['tag_name'], dry_run=True)
                self.assertEqual(run('rev-parse','HEAD'), initial)
                with patch.dict(os.environ,{'GITHUB_TOKEN':'unit-test-only','GITHUB_REPOSITORY':feed.REPO}), \
                     patch.object(feed,'urlopen',return_value=io.BytesIO(json.dumps(release).encode())), \
                     patch.object(feed,'download',side_effect=download):
                    feed.promote(release['tag_name'])
                final = json.loads(Path('manifest.json').read_text())
                self.assertEqual(final['version'], metadata['version'])
                self.assertEqual(run('rev-list','--count','HEAD'), '3')
                self.assertNotIn('/main/', final['url'])
                self.assertEqual(final['approved'], True)
                with patch.dict(os.environ,{'GITHUB_TOKEN':'unit-test-only','GITHUB_REPOSITORY':feed.REPO}), \
                     patch.object(feed,'urlopen',return_value=io.BytesIO(json.dumps(release).encode())), \
                     patch.object(feed,'download',side_effect=download):
                    feed.promote(release['tag_name'])
                self.assertEqual(run('rev-list','--count','HEAD'), '3')
            finally:
                os.chdir(previous)


if __name__ == '__main__':
    unittest.main()
