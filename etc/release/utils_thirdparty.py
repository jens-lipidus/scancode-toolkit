#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# Copyright (c) nexB Inc. and others. All rights reserved.
# http://nexb.com and https://github.com/nexB/scancode-toolkit/
# The ScanCode software is licensed under the Apache License version 2.0.
# ScanCode is a trademark of nexB Inc.
#
# You may not use this software except in compliance with the License.
# You may obtain a copy of the License at: http://apache.org/licenses/LICENSE-2.0
# Unless required by applicable law or agreed to in writing, software distributed
# under the License is distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR
# CONDITIONS OF ANY KIND, either express or implied. See the License for the
# specific language governing permissions and limitations under the License.
#
#  ScanCode is a free software code scanning tool from nexB Inc. and others.
#  Visit https://github.com/nexB/scancode-toolkit/ for support and download.

from collections import defaultdict
import itertools
import operator
import os
import re
import subprocess
import tarfile
import tempfile
import time

import attr
import packageurl
import utils_pip_compatibility_tags
import utils_pypi_supported_tags
import requests
import saneyaml

from commoncode import fileutils
from commoncode.hash import multi_checksums
from packaging import tags as packaging_tags
from packaging import version as packaging_version
from utils_requirements import load_requirements

"""
Utilities to manage Python thirparty libraries source, binaries and metadata in
local directories and remote repositories.

- update pip requirement files from installed packages for prod. and dev.
- build and save wheels for all required packages
- also build variants for wheels with native code for all each supported
  operating systems (Linux, macOS, Windows) and Python versions (3.x)
  combinations using remote Ci jobs
- collect source distributions for all required packages
- keep in sync wheels, distributions, ABOUT and LICENSE files to a PyPI-like
  repository (using GitHub)
- create, update and fetch ABOUT, NOTICE and LICENSE metadata for all distributions


Approach
--------

The processing is organized around these key objects:

- A PyPiPackage represents a PyPI package with its name and version. It tracks
  the downloadable Distribution objects for that version:

  - one Sdist source Distribution object
  - a list of Wheel binary Distribution objects

- A Distribution (either a Wheel or Sdist) is identified by and created from its
  filename. It also has the metadata used to populate an .ABOUT file and
  document origin and license. A Distribution can be fetched from Repository.
  Metadata can be loaded from and dumped to ABOUT files and optionally from
  DejaCode package data.

- An Environment is a combination of a Python version and operating system.
  A Wheel Distribution also has Python/OS tags is supports and these can be
  supported in a given Environment.

- Paths or URLs to "filenames" live in a Repository, either a plain
  LinksRepository (an HTML page listing URLs or a local directory) or a
  PypiRepository (a PyPI simple index where each package name has an HTML page
  listing URLs to all distribution types and versions).
  Repositories and Distributions are related through filenames.


 The Wheel models code is partially derived from the mit-licensed pip and the
 Distribution/Wheel/Sdist design has been heavily inspired by the packaging-
 dists library https://github.com/uranusjr/packaging-dists by Tzu-ping Chung
"""

# Supported environments
PYTHON_VERSIONS = '36', '37', '38', '39',
PYTHON_DOT_VERSIONS = tuple('.'.join(v) for v in PYTHON_VERSIONS)

ABIS_BY_PYTHON_VERSION = {
    '36':['cp36', 'cp36m'],
    '37':['cp37', 'cp37m'],
    '38':['cp38', 'cp38m'],
    '39':['cp39', 'cp39m'],
}

PLATFORMS_BY_OS = {
    'linux': [
        'linux_x86_64',
        'manylinux1_x86_64',
        'manylinux2014_x86_64',
        'manylinux2010_x86_64',
    ],
    'macos': [
        'macosx_10_6_intel', 'macosx_10_6_x86_64',
        'macosx_10_9_intel', 'macosx_10_9_x86_64',
        'macosx_10_10_intel', 'macosx_10_10_x86_64',
        'macosx_10_11_intel', 'macosx_10_11_x86_64',
        'macosx_10_12_intel', 'macosx_10_12_x86_64',
        'macosx_10_13_intel', 'macosx_10_13_x86_64',
        'macosx_10_14_intel', 'macosx_10_14_x86_64',
        'macosx_10_15_intel', 'macosx_10_15_x86_64',
    ],
    'windows': [
        'win_amd64',
    ],
}

THIRDPARTY_DIR = 'thirdparty'

REMOTE_BASE_URL = 'https://github.com'
REMOTE_LINKS_URL = 'https://github.com/nexB/thirdparty-packages/releases/pypi'
REMOTE_HREF_PREFIX = '/nexB/thirdparty-packages/releases/download/'
REMOTE_BASE_DOWNLOAD_URL = 'https://github.com/nexB/thirdparty-packages/releases/download/pypi'

EXTENSIONS_APP = '.pyz',
EXTENSIONS_SDIST = '.tar.gz', '.tar.bz2', '.zip', '.tar.xz',
EXTENSIONS_INSTALLABLE = EXTENSIONS_SDIST + ('.whl',)
EXTENSIONS_ABOUT = '.ABOUT', '.LICENSE', '.NOTICE',
EXTENSIONS = EXTENSIONS_INSTALLABLE + EXTENSIONS_ABOUT + EXTENSIONS_APP

PYPI_SIMPLE_URL = 'https://pypi.org/simple'

LICENSEDB_API_URL = 'https://scancode-licensedb.aboutcode.org'

################################################################################
#
# main entry point
#
################################################################################


def fetch_wheels(environment=None, requirement_file='requirements.txt', dest_dir=THIRDPARTY_DIR):
    """
    Download all of the wheel of packages listed in the `requirement_file`
    requirements file into `dest_dir` directory.

    Only get wheels for the `environment` Enviromnent constraints. If the
    provided `environment` is None then the current Python interpreter
    environment is used implicitly.
    Use direct downloads from our remote repo exclusively.
    Yield tuples of (PypiPackage, error) where is None on success
    """
    missed = []
    rrp = list(get_required_remote_packages(requirement_file))

    for name, version, package in rrp:
        if not package:
            missed.append((name, version,))
            yield None, f'Missing package in remote repo: {name}=={version}'

        else:
            fetched = package.fetch_wheel(environment=environment, dest_dir=dest_dir)
            error = f'Failed to fetch' if not fetched else None
            yield package, error

    if missed:
        rr = get_remote_repo()
        print()
        print(f'===============> Missed some packages')
        for n, v in missed:
            print(f'Missed package in remote repo: {n}=={v} from:')
            for pv in rr.get_versions(n):
                print(pv)


def fetch_sources(requirement_file='requirements.txt', dest_dir=THIRDPARTY_DIR,):
    """
    Download all of the dependent package sources listed in the `requirement`
    requirements file into `dest_dir` directory.

    Use direct downloads to achieve this (not pip download). Use only the
    packages found in our remote repo. Yield tuples of
    (PypiPackage, error message) for each package where error message will empty on
    success
    """
    missed = []
    rrp = list(get_required_remote_packages(requirement_file))
    for name, version, package in rrp:
        if not package:
            missed.append((name, version,))
            yield None, f'Missing package in remote repo: {name}=={version}'

        elif not package.sdist:
            yield package, f'Missing sdist in links'

        else:
            fetched = package.fetch_sdist(dest_dir=dest_dir)
            error = f'Failed to fetch' if not fetched else None
            yield package, error


def fetch_venv_abouts_and_licenses(dest_dir=THIRDPARTY_DIR):
    """
    Download to `est_dir` a virtualenv.pyz app, and all .ABOUT, license and
    notice files for all packages in `dest_dir`
    """
    remote_repo = get_remote_repo()
    paths_or_urls = remote_repo.get_links()

    fetch_and_save_filename_from_paths_or_urls(
        filename='virtualenv.pyz',
        dest_dir=dest_dir,
        paths_or_urls=paths_or_urls,
        as_text=False,
    )

    fetch_abouts(dest_dir=dest_dir, paths_or_urls=paths_or_urls)
    fetch_license_texts_and_notices(dest_dir=dest_dir, paths_or_urls=paths_or_urls)


def fetch_package_wheel(name, version, environment, dest_dir=THIRDPARTY_DIR):
    """
    Fetch the binary wheel for package `name` and `version` and save in
    `dest_dir`. Use the provided `environment` Environment to determine which
    specific wheel to fetch.

    Return the fetched wheel file name on success or None if it was not fetched.
    Trying fetching from our own remote repo, then from PyPI.
    """
    wheel_file = None
    remote_package = get_remote_package(name, version)
    if remote_package:
        wheel_file = remote_package.fetch_wheel(environment, dest_dir)
    if not wheel_file:
        pypi_package = get_pypi_package(name, version)
        if pypi_package:
            wheel_file = pypi_package.fetch_wheel(environment, dest_dir)
    return wheel_file

################################################################################
#
# Core models
#
################################################################################


@attr.attributes
class NameVer:
    name = attr.ib(
        type=str,
        metadata=dict(help='Python package name, lowercase and normalized.'),
    )

    version = attr.ib(
        type=str,
        metadata=dict(help='Python package version string.'),
    )

    @property
    def normalized_name(self):
        return NameVer.normalize_name(self.name)

    @staticmethod
    def normalize_name(name):
        """
        Return a tuple of normalized package name per PEP503, and copied from
        https://www.python.org/dev/peps/pep-0503/#id4
        """
        return name and re.sub(r"[-_.]+", "-", name).lower() or name

    @property
    def name_ver(self):
        return f'{self.name}-{self.version}'

    @classmethod
    def sorter_by_name_version(cls, namever):
        return namever.normalized_name, packaging_version.parse(namever.version)

    @classmethod
    def sorted(cls, namevers):
        return sorted(namevers, key=cls.sorter_by_name_version)


@attr.attributes
class Distribution(NameVer):

    filename = attr.ib(
        repr=False,
        type=str,
        default='',
        metadata=dict(help='File name.'),
    )

    path_or_url = attr.ib(
        repr=False,
        type=str,
        default='',
        metadata=dict(help='Path or download URL.'),
    )

    sha1 = attr.ib(
        repr=False,
        type=str,
        default='',
        metadata=dict(help='SHA1 checksum.'),
    )

    md5 = attr.ib(
        repr=False,
        type=int,
        default=0,
        metadata=dict(help='MD5 checksum.'),
    )

    type = attr.ib(
        repr=False,
        type=str,
        default='pypi',
        metadata=dict(help='Package type'),
    )

    namespace = attr.ib(
        repr=False,
        type=str,
        default='',
        metadata=dict(help='Package URL namespace'),
    )

    qualifiers = attr.ib(
        repr=False,
        type=dict,
        default=attr.Factory(dict),
        metadata=dict(help='Package URL qualifiers'),
    )

    subpath = attr.ib(
        repr=False,
        type=str,
        default='',
        metadata=dict(help='Package URL subpath'),
    )

    size = attr.ib(
        repr=False,
        type=str,
        default='',
        metadata=dict(help='Size in bytes.'),
    )

    primary_language = attr.ib(
        repr=False,
        type=str,
        default='Python',
        metadata=dict(help='Primary Programming language.'),
    )

    description = attr.ib(
        repr=False,
        type=str,
        default='',
        metadata=dict(help='Description.'),
    )

    homepage_url = attr.ib(
        repr=False,
        type=str,
        default='',
        metadata=dict(help='Homepage URL'),
    )

    notes = attr.ib(
        repr=False,
        type=str,
        default='',
        metadata=dict(help='Notes.'),
    )

    copyright = attr.ib(
        repr=False,
        type=str,
        default='',
        metadata=dict(help='Copyright.'),
    )

    license_expression = attr.ib(
        repr=False,
        type=str,
        default='',
        metadata=dict(help='License expression'),
    )

    licenses = attr.ib(
        repr=False,
        type=list,
        default=attr.Factory(list),
        metadata=dict(help='List of license mappings.'),
    )

    notice_text = attr.ib(
        repr=False,
        type=str,
        default='',
        metadata=dict(help='Notice text'),
    )

    extra_data = attr.ib(
        repr=False,
        type=dict,
        default=attr.Factory(dict),
        metadata=dict(help='Extra data'),
    )

    @property
    def package_url(self):
        fields = {
            k: v for k, v in attr.asdict(self).items()
            if k in packageurl._components
        }

        return packageurl.PackageURL(**fields)

    @property
    def download_url(self):
        if self.path_or_url and self.path_or_url.startswith('https://'):
            return self.path_or_url
        else:
            return self.get_best_download_url()

    @property
    def about_filename(self):
        return f'{self.filename}.ABOUT'

    @property
    def about_download_url(self):
        return self.build_remote_download_url(self.about_filename)

    @property
    def notice_filename(self):
        return f'{self.filename}.NOTICE'

    @property
    def notice_download_url(self):
        return self.build_remote_download_url(self.notice_filename)

    @classmethod
    def from_path_or_url(cls, path_or_url):
        """
        Return a distribution built from the data found in the filename of a
        `path_or_url` string. Raise an exception if this is not a valid
        filename.
        """
        filename = os.path.basename(path_or_url.strip('/'))
        dist = cls.from_filename(filename)
        dist.path_or_url = path_or_url
        return dist

    @classmethod
    def get_dist_class(cls, filename):
        if filename.endswith('.whl'):
            return Wheel
        elif filename.endswith(('.zip', '.tar.gz',)):
            return Sdist
        raise InvalidDistributionFilename(filename)

    @classmethod
    def from_filename(cls, filename):
        """
        Return a distribution built from the data found in a `filename` string.
        Raise an exception if this is not a valid filename
        """
        clazz = cls.get_dist_class(filename)
        return clazz.from_filename(filename)

    @classmethod
    def from_data(cls, data, keep_extra=False):
        """
        Return a distribution built from a `data` mapping.
        """
        filename = data['filename']
        dist = cls.from_filename(filename)
        dist.update(data, keep_extra=keep_extra)
        return dist

    @classmethod
    def from_dist(cls, data, dist):
        """
        Return a distribution built from a `data` mapping and update it with data
        from another dist Distribution. Return None if it cannot be created
        """
        if data.get('type') != dist.type or data.get('name') != dist.name:
            # We can only create from a dist of the same package
            return

        data = dict(data)

        fields_to_carry_over = [
            'license_expression',
            'copyright',
            'description',
            'homepage_url',
            'primary_language',
            'notice_text',
        ]
        dist_data = {k: v for k, v in dist.to_dict().items() if k in fields_to_carry_over}
        data.update(dist_data)
        return cls.from_data(data)

    @classmethod
    def build_remote_download_url(cls, filename, base_url=REMOTE_BASE_DOWNLOAD_URL):
        """
        Return a direct download URL for a file in our remote repo
        """
        return f'{base_url}/{filename}'

    def get_best_download_url(self):
        """
        Return the best download URL for this distribution where best means that
        PyPI is better and our remote urls are second.
        """
        name = self.normalized_name
        version = self.version
        filename = self.filename

        pypi_package = get_pypi_package(name=name, version=version)
        if pypi_package:
            pypi_url = pypi_package.get_url_for_filename(filename)
            if pypi_url:
                return pypi_url

        remote_package = get_remote_package(name=name, version=version)
        if remote_package:
            remote_url = remote_package.get_url_for_filename(filename)
            if remote_url:
                return remote_url

    def purl_identifiers(self, skinny=False):
        """
        Return a mapping of non-empty identifier name/values for each each purl
        fields.
        If skinny is True, only inlucde type, namespace and name.
        """
        identifiers = dict(
            type=self.type,
            namespace=self.namespace,
            name=self.name,
        )

        if not skinny:
            identifiers.update(
                version=self.version,
                subpath=self.subpath,
                qualifiers=self.qualifiers,
            )

        return {k: v for k, v in sorted(identifiers.items()) if v}

    def identifiers(self, purl_as_fields=True):
        """
        Return a mapping of non-empty identifier name/values.
        Return each purl fields separately if purl_as_fields is True.
        Otherwise return a package_url string for the purl.
        """
        if purl_as_fields:
            identifiers = self.purl_identifiers()
        else:
            identifiers = dict(package_url=self.package_url)

        identifiers.update(
            download_url=self.download_url,
            filename=self.filename,
            md5=self.md5,
            sha1=self.sha1,
            package_url=self.package_url,
        )

        return {k: v for k, v in sorted(identifiers.items()) if v}

    def to_about(self):
        """
        Return a mapping of ABOUT data from this distribution fields.
        """
        about_data = dict(
            about_resource=self.filename,
            checksum_md5=self.md5,
            checksum_sha1=self.sha1,
            copyright=self.copyright,
            description=self.description,
            download_url=self.download_url,
            homepage_url=self.homepage_url,
            license_expression=self.license_expression,
            name=self.name,
            namespace=self.namespace,
            notes=self.notes,
            notice_file=self.notice_filename if self.notice_text else '',
            package_url=self.package_url,
            primary_language=self.primary_language,
            qualifiers=self.qualifiers,
            size=self.size,
            subpath=self.subpath,
            type=self.type,
            version=self.version,
        )

        about_data.update(self.extra_data)
        about_data = {k: v for k, v in sorted(about_data.items()) if v}
        return about_data

    def to_dict(self):
        """
        Return a mapping data from this distribution.
        """
        return {k: v for k, v in  attr.asdict(self).items() if v}

    def save_about_and_notice_files(self, dest_dir=THIRDPARTY_DIR):
        """
        Save a .ABOUT file to `dest_dir`. Include a .NOTICE file if there is a
        notice_text.
        """
        with open(os.path.join(dest_dir, self.about_filename), 'w') as fo:
            fo.write(saneyaml.dump(self.to_about()))

        if self.notice_text and self.notice_text.strip():
            with open(os.path.join(dest_dir, self.notice_filename), 'w') as fo:
                fo.write(self.notice_text)

    def load_about_data(self, about_filename_or_data, dest_dir=THIRDPARTY_DIR):
        """
        Update self with ABOUT data loaded from an `about_filename_or_data`
        which is either a .ABOUT file in `dest_dir` or an ABOUT data mapping.
        Load the notice_text if present from dest_dir.
        """
        if isinstance(about_filename_or_data, str):
            # that's a path
            with open(about_filename_or_data) as fi:
                about_data = saneyaml.load(fi.read())
        else:
            about_data = about_filename_or_data

        notice_text = about_data.pop('notice_text', None)
        notice_file = about_data.pop('notice_file', None)
        if notice_text:
            about_data['notice_text'] = notice_text
        elif notice_file:
            with open(os.path.join(dest_dir, notice_file)) as fi:
                about_data['notice_text'] = fi.read()

        self.update(about_data, keep_extra=True)

    def fetch_and_update_with_remote_about_data(self, dest_dir=THIRDPARTY_DIR):
        """
        Fetch and update self with "remote" ABOUT file and NOTICE file if any.
        """
        try:
            about_text = fetch_content_from_path_or_url_through_cache(self.about_download_url)
        except RemoteNotFetchedException:
            return

        if not about_text:
            return

        about_data = saneyaml.load(about_text)

        notice_file = about_data.pop('notice_file', None)
        if notice_file:
            try:
                notice_text = fetch_content_from_path_or_url_through_cache(
                    self.notice_download_url)
                about_data['notice_text'] = notice_text
            except RemoteNotFetchedException:
                pass

        self.update(about_data, keep_extra=True)

    def update(self, data, overwrite=False, keep_extra=True):
        """
        Update self with a mapping of data. Keep unknown data as extra_data
        if keep_extra is True. If overwrite is True, overwrite self with `data`
        Return True if any data was updated.
        """
        updated = False
        extra = {}
        for k, v in data.items():
            if isinstance(v, str):
                v = v.strip()
            if not v:
                continue

            if hasattr(self, k):
                value = getattr(self, k, None)
                if not value or (overwrite and value != v):
                    setattr(self, k, v)
                    updated = True

            elif keep_extra:
                # note that we always overwrite extra
                extra[k] = v
                updated = True

        self.extra_data.update(extra)

        return updated


class InvalidDistributionFilename(Exception):
    pass


@attr.attributes
class Sdist(Distribution):

    extension = attr.ib(
        repr=False,
        type=str,
        default='',
        metadata=dict(help='File extension, including leading dot.'),
    )

    @classmethod
    def from_filename(cls, filename):
        """
        Return a sdist object built from a filename.
        Raise an exception if this is not a valid sdist filename
        """
        name_ver = None
        extension = None

        for ext in EXTENSIONS_SDIST:
            if filename.endswith(ext):
                name_ver, extension, _ = filename.rpartition(ext)
                break

        if not extension or not name_ver:
            raise InvalidDistributionFilename(filename)

        name, _, version = name_ver.rpartition('-')

        if not name or not version:
            raise InvalidDistributionFilename(filename)

        return cls(
            name=name,
            version=version,
            extension=extension,
            filename=filename,
        )

    def to_filename(self):
        """
        Return an sdist filename reconstructed from its fields (that may not be
        the same as the original filename.)
        """
        return f'{self.name}-{self.version}.{self.extension}'


@attr.attributes
class Wheel(Distribution):

    """
    Represents a wheel file.

    Copied and heavily modified from pip-20.3.1 copied from pip-20.3.1
    pip/_internal/models/wheel.py

    name: pip compatibility tags
    version: 20.3.1
    download_url: https://github.com/pypa/pip/blob/20.3.1/src/pip/_internal/models/wheel.py
    copyright: Copyright (c) 2008-2020 The pip developers (see AUTHORS.txt file)
    license_expression: mit
    notes: copied from pip-20.3.1 pip/_internal/models/wheel.py

    Copyright (c) 2008-2020 The pip developers (see AUTHORS.txt file)

    Permission is hereby granted, free of charge, to any person obtaining
    a copy of this software and associated documentation files (the
    "Software"), to deal in the Software without restriction, including
    without limitation the rights to use, copy, modify, merge, publish,
    distribute, sublicense, and/or sell copies of the Software, and to
    permit persons to whom the Software is furnished to do so, subject to
    the following conditions:

    The above copyright notice and this permission notice shall be
    included in all copies or substantial portions of the Software.

    THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
    EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
    MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
    NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE
    LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION
    OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION
    WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
    """

    wheel_file_re = re.compile(
        r"""^(?P<namever>(?P<name>.+?)-(?P<ver>.*?))
        ((-(?P<build>\d[^-]*?))?-(?P<pyvers>.+?)-(?P<abis>.+?)-(?P<plats>.+?)
        \.whl)$""",
        re.VERBOSE
    )

    build = attr.ib(
        type=str,
        default='',
        metadata=dict(help='Python wheel build.'),
    )

    python_versions = attr.ib(
        type=list,
        default=attr.Factory(list),
        metadata=dict(help='List of wheel Python version tags.'),
    )

    abis = attr.ib(
        type=list,
        default=attr.Factory(list),
        metadata=dict(help='List of wheel ABI tags.'),
    )

    platforms = attr.ib(
        type=list,
        default=attr.Factory(list),
        metadata=dict(help='List of wheel platform tags.'),
    )

    tags = attr.ib(
        repr=False,
        type=set,
        default=attr.Factory(set),
        metadata=dict(help='Set of all tags for this wheel.'),
    )

    @classmethod
    def from_filename(cls, filename):
        """
        Return a wheel object built from a filename.
        Raise an exception if this is not a valid wheel filename
        """
        wheel_info = cls.wheel_file_re.match(filename)
        if not wheel_info:
            raise InvalidDistributionFilename(filename)

        name = wheel_info.group('name').replace('_', '-')
        # we'll assume "_" means "-" due to wheel naming scheme
        # (https://github.com/pypa/pip/issues/1150)
        version = wheel_info.group('ver').replace('_', '-')
        build = wheel_info.group('build')
        python_versions = wheel_info.group('pyvers').split('.')
        abis = wheel_info.group('abis').split('.')
        platforms = wheel_info.group('plats').split('.')

        # All the tag combinations from this file
        tags = {
            packaging_tags.Tag(x, y, z) for x in python_versions
            for y in abis for z in platforms
        }

        return cls(
            filename=filename,
            name=name,
            version=version,
            build=build,
            python_versions=python_versions,
            abis=abis,
            platforms=platforms,
            tags=tags,
        )

    def is_supported_by_tags(self, tags):
        """
        Return True is this wheel is compatible with one of a list of PEP 425 tags.
        """
        return not self.tags.isdisjoint(tags)

    def is_supported_by_environment(self, environment):
        """
        Return True if this wheel is compatible with the Environment
        `environment`.
        """
        return  not self.is_supported_by_tags(environment.tags)

    def to_filename(self):
        """
        Return a wheel filename reconstructed from its fields (that may not be
        the same as the original filename.)
        """
        build = f'-{self.build}' if self.build else ''
        pyvers = '.'.join(self.python_versions)
        abis = '.'.join(self.abis)
        plats = '.'.join(self.platforms)
        return f'{self.name}-{self.version}{build}-{pyvers}-{abis}-{plats}.whl'

    def is_pure(self):
        """
        Return True if wheel `filename` is for a "pure" wheel e.g. a wheel that
        runs on all Pythons 3 and all OSes.

        For example::

        >>> Wheel.from_filename('aboutcode_toolkit-5.1.0-py2.py3-none-any.whl').is_pure()
        True
        >>> Wheel.from_filename('beautifulsoup4-4.7.1-py3-none-any.whl').is_pure()
        True
        >>> Wheel.from_filename('beautifulsoup4-4.7.1-py2-none-any.whl').is_pure()
        False
        >>> Wheel.from_filename('bitarray-0.8.1-cp36-cp36m-win_amd64.whl').is_pure()
        False
        >>> Wheel.from_filename('extractcode_7z-16.5-py2.py3-none-macosx_10_13_intel.whl').is_pure()
        False
        >>> Wheel.from_filename('future-0.16.0-cp36-none-any.whl').is_pure()
        False
        >>> Wheel.from_filename('foo-4.7.1-py3-none-macosx_10_13_intel.whl').is_pure()
        False
        >>> Wheel.from_filename('future-0.16.0-py3-cp36m-any.whl').is_pure()
        False
        """
        return (
            'py3' in self.python_versions
            and 'none' in self.abis
            and 'any' in self.platforms
        )


def is_pure_wheel(filename):
    try:
        return Wheel.from_filename(filename).is_pure()
    except:
        return False


@attr.attributes
class PypiPackage(NameVer):
    """
    A Python package with its "distributions", e.g. wheels and source
    distribution , ABOUT files and licenses or notices.
    """
    sdist = attr.ib(
        repr=False,
        type=str,
        default='',
        metadata=dict(help='Sdist source distribution for this package.'),
    )

    wheels = attr.ib(
        repr=False,
        type=list,
        default=attr.Factory(list),
        metadata=dict(help='List of Wheel for this package'),
    )

    @property
    def specifier(self):
        """
        A requirement specifier for this package
        """
        if self.version:
            return f'{self.name}=={self.version}'
        else:
            return self.name

    def get_supported_wheels(self, environment):
        """
        Yield all the Wheel of this package supported and compatible with the
        Environment `environment`.
        """
        envt_tags = environment.tags()
        for wheel in self.wheels:
            if wheel.is_supported_by_tags(envt_tags):
                yield wheel

    @classmethod
    def package_from_dists(cls, dists):
        """
        Return a new PypiPackage built from an iterable of Wheels and Sdist
        objects all for the same package name and version.

        For example:
        >>> w1 = Wheel(name='bitarray', version='0.8.1', build='',
        ...    python_versions=['cp36'], abis=['cp36m'],
        ...    platforms=['linux_x86_64'])
        >>> w2 = Wheel(name='bitarray', version='0.8.1', build='',
        ...    python_versions=['cp36'], abis=['cp36m'],
        ...    platforms=['macosx_10_9_x86_64', 'macosx_10_10_x86_64'])
        >>> sd = Sdist(name='bitarray', version='0.8.1')
        >>> package = PypiPackage.package_from_dists(dists=[w1, w2, sd])
        >>> assert package.name == 'bitarray'
        >>> assert package.version == '0.8.1'
        >>> assert package.sdist == sd
        >>> assert package.wheels == [w1, w2]
        """
        dists = list(dists)
        if not dists:
            return

        reference_dist = dists[0]
        normalized_name = reference_dist.normalized_name
        version = reference_dist.version

        package = PypiPackage(name=normalized_name, version=version)

        for dist in dists:
            if dist.normalized_name != normalized_name or dist.version != version:
                raise Exception(
                    f'Inconsistent dist name and version in set:: {dist} '
                    f'Expected name: {normalized_name} and version: {version} ')

            if isinstance(dist, Sdist):
                package.sdist = dist

            elif isinstance(dist, Wheel):
                package.wheels.append(dist)

            else:
                raise Exception(f'Unknown distribution type: {dist}')

        return package

    @classmethod
    def packages_from_one_path_or_url(cls, path_or_url):
        """
        Yield PypiPackages built from files found in at directory path or the
        URL to an HTML page (that will be fetched).
        """
        extracted_paths_or_urls = get_paths_or_urls(path_or_url)
        return cls.packages_from_many_paths_or_urls(extracted_paths_or_urls)

    @classmethod
    def packages_from_many_paths_or_urls(cls, paths_or_urls):
        """
        Yield PypiPackages built from a list of of paths or URLs.
        """
        dists = cls.get_dists(paths_or_urls)
        dists = NameVer.sorted(dists)

        for _projver, dists_of_package in itertools.groupby(
            dists, key=NameVer.sorter_by_name_version,
        ):
            yield PypiPackage.package_from_dists(dists_of_package)

    @classmethod
    def get_versions_from_path_or_url(cls, name, path_or_url):
        """
        Return a subset list from a list of PypiPackages version at `path_or_url`
        that match PypiPackage `name`.
        """
        packages = cls.packages_from_one_path_or_url(path_or_url)
        return cls.get_versions(name, packages)

    @classmethod
    def get_versions(cls, name, packages):
        """
        Return a subset list of package versions from a list of `packages` that
        match PypiPackage `name`.
        The list is sorted by version from oldest to most recent.
        """
        norm_name = NameVer.normalize_name(name)
        versions = [p for p in packages if p.normalized_name == norm_name]
        return cls.sorted(versions)

    @classmethod
    def get_latest_version(cls, name, packages):
        """
        Return the latest version of PypiPackage `name` from a list of `packages`.
        """
        versions = cls.get_versions(name, packages)
        return versions[-1]

    @classmethod
    def get_outdated_versions(cls, name, packages):
        """
        Return all versions except the latest version of PypiPackage `name` from a
        list of `packages`.
        """
        versions = cls.get_versions(name, packages)
        return versions[:-1]

    @classmethod
    def get_name_version(cls, name, version, packages):
        """
        Return the PypiPackage with `name` and `version` from a list of `packages`
        or None if it is not found.
        If `version` is None, return the latest version found.
        """
        if version is None:
            return cls.get_latest_version(name, packages)

        nvs = [p for p in cls.get_versions(name, packages) if p.version == version]

        if not nvs:
            return

        if len(nvs) == 1:
            return nvs[0]

        raise Exception(f'More than one PypiPackage with {name}=={version}')

    def fetch_wheel(self, environment=None, dest_dir=THIRDPARTY_DIR):
        """
        Download a binary wheel of this package matching the `environment`
        Enviromnent constraints into `dest_dir` directory.

        Return the wheel file name if was fetched, None otherwise.

        If the provided `environment` is None then the current Python
        interpreter environment is used implicitly.
        """
        fetched_wheel_filename = None

        for wheel in self.get_supported_wheels(environment):

            print(
                'Fetching environment-supported wheel for:',
                self.name, self.version,
                '--> filename:', wheel.filename,
            )

            fetch_and_save_path_or_url(
                filename=wheel.filename,
                path_or_url=wheel.path_or_url,
                dest_dir=dest_dir,
                as_text=False,
            )

            fetched_wheel_filename = wheel.filename
            # TODO: what if there is more than one?
            break

        return fetched_wheel_filename

    def fetch_sdist(self, dest_dir=THIRDPARTY_DIR):
        """
        Download the source distribution into `dest_dir` directory. Return the
        fetched filename if it was fetched, False otherwise.
        """
        if self.sdist:
            assert self.sdist.filename
            print('Fetching source for package:', self.name, self.version)
            fetch_and_save_path_or_url(
                filename=self.sdist.filename,
                dest_dir=dest_dir,
                path_or_url=self.sdist.path_or_url,
                as_text=False,
            )
            print(' --> file:', self.sdist.filename)
            return self.sdist.filename
        else:
            print(f'Missing sdist for: {self.name}=={self.version}')
            return False

    def delete_files(self, dest_dir=THIRDPARTY_DIR):
        """
        Delete all PypiPackage files from `dest_dir` including wheels, sdist and
        their ABOUT files. Note that we do not delete licenses since they can be
        shared by several packages: therefore this would be done elsewhere in a
        function that is aware of all used licenses.
        """
        for to_delete in self.wheels + [self.dist]:
            if not to_delete:
                continue
            tdfn = to_delete.filename
            for deletable in [tdfn, f'{tdfn}.ABOUT', f'{tdfn}.NOTICE']:
                target = os.path.join(dest_dir, deletable)
                if os.path.exists(target):
                    fileutils.delete(target)

    @classmethod
    def get_dists(cls, paths_or_urls):
        """
        Return a list of Distribution given a list of
        `paths_or_urls` to wheels or source distributions.

        Each Distribution receives two extra attributes:
            - the path_or_url it was created from
            - its filename

        For example:
        >>> paths_or_urls ='''
        ...     /home/foo/bitarray-0.8.1-cp36-cp36m-linux_x86_64.whl
        ...     bitarray-0.8.1-cp36-cp36m-macosx_10_9_x86_64.macosx_10_10_x86_64.whl
        ...     bitarray-0.8.1-cp36-cp36m-win_amd64.whl
        ...     httsp://example.com/bar/bitarray-0.8.1.tar.gz
        ...     bitarray-0.8.1.tar.gz.ABOUT bit.LICENSE'''.split()
        >>> result = list(PypiPackage.get_dists(paths_or_urls))
        >>> for r in results:
        ...    r.filename = ''
        ...    r.path_or_url = ''
        >>> expected = [
        ...     Wheel(name='bitarray', version='0.8.1', build='',
        ...         python_versions=['cp36'], abis=['cp36m'],
        ...         platforms=['linux_x86_64']),
        ...     Wheel(name='bitarray', version='0.8.1', build='',
        ...         python_versions=['cp36'], abis=['cp36m'],
        ...         platforms=['macosx_10_9_x86_64', 'macosx_10_10_x86_64']),
        ...     Wheel(name='bitarray', version='0.8.1', build='',
        ...         python_versions=['cp36'], abis=['cp36m'],
        ...         platforms=['win_amd64']),
        ...     Sdist(name='bitarray', version='0.8.1')
        ... ]
        >>> assert expected == result
        """
        installable = [f for f in paths_or_urls if f.endswith(EXTENSIONS_INSTALLABLE)]
        for path_or_url in installable:
            try:
                yield Distribution.from_path_or_url(path_or_url)
            except InvalidDistributionFilename:
                print(f'Skipping invalid distribution from: {path_or_url}')
                continue

    def get_distributions(self):
        """
        Yield all distributions available for this PypiPackage
        """
        if self.sdist:
            yield self.sdist
        for wheel in self.wheels:
            yield wheel

    def get_url_for_filename(self, filename):
        """
        Return the URL for this filename or None.
        """
        for dist in self.get_distributions():
            if dist.filename == filename:
                return dist.path_or_url


@attr.attributes
class Environment:
    """
    An Environment describes a target installation environment with its
    supported Python version, ABI, platform, implementation and related
    attributes. We can use these to pass as `pip download` options and force
    fetching only the subset of packages that match these Environment
    constraints as opposed to the current running Python interpreter
    constraints.
    """

    python_version = attr.ib(
        type=str,
        default='',
        metadata=dict(help='Python version supported by this environment.'),
    )

    operating_system = attr.ib(
        type=str,
        default='',
        metadata=dict(help='operating system supported by this environment.'),
    )

    implementation = attr.ib(
        type=str,
        default='cp',
        metadata=dict(help='Python implementation supported by this environment.'),
    )

    abis = attr.ib(
        type=list,
        default=attr.Factory(list),
        metadata=dict(help='List of  ABI tags supported by this environment.'),
    )

    platforms = attr.ib(
        type=list,
        default=attr.Factory(list),
        metadata=dict(help='List of platform tags supported by this environment.'),
    )

    @property
    def python_dot_version(self):
        return '.'.join(self.python_version)

    @classmethod
    def from_pyver_and_os(cls, python_version, operating_system):
        if '.' in python_version:
            python_version = ''.join(python_version.split('.'))

        return cls(
            python_version=python_version,
            implementation='cp',
            abis=ABIS_BY_PYTHON_VERSION[python_version],
            platforms=PLATFORMS_BY_OS[operating_system],
            operating_system=operating_system,
        )

    def get_pip_cli_options(self):
        """
        Return a list of pip command line options for this environment.
        """
        options = [
            '--python-version', self.python_version,
            '--implementation', self.implementation,
            '--abi', self.abi,
        ]
        for platform in self.platforms:
            options.extend(['--platform', platform])
        return options

    def tags(self):
        """
        Return a set of all the PEP425 tags supported by this environment.
        """
        return set(utils_pip_compatibility_tags.get_supported(
            version=self.python_version or None,
            impl=self.implementation or None,
            platforms=self.platforms or None,
            abis=self.abis or None,
        ))

################################################################################
#
# PyPI repo and link index for package wheels and sources
#
################################################################################


@attr.attributes
class Repository:
    """
    A PyPI or links Repository of Python packages: wheels, sdist, ABOUT, etc.
    """

    packages_by_normalized_name = attr.ib(
        type=dict,
        default=attr.Factory(lambda: defaultdict(list)),
        metadata=dict(help=
            'Mapping of {package name: [package objects]} available in this repo'),
    )

    packages_by_normalized_name_version = attr.ib(
        type=dict,
        default=attr.Factory(dict),
        metadata=dict(help=
            'Mapping of {(name, version): package object} available in this repo'),
    )

    def get_links(self, *args, **kwargs):
        raise NotImplementedError()

    def get_versions(self, name):
        """
        Return a list of all available PypiPackage version for this package name.
        The list may be empty.
        """
        raise NotImplementedError()

    def get_package(self, name, version):
        """
        Return the PypiPackage with name and version or None.
        """
        raise NotImplementedError()

    def get_latest_version(self, name):
        """
        Return the latest PypiPackage version for this package name or None.
        """
        raise NotImplementedError()


@attr.attributes
class LinksRepository(Repository):
    """
    Represents a simple links repository which is either a local directory with
    Python wheels and sdist or a remote URL to an HTML with links to these.
    (e.g. suitable for use with pip --find-links).
    """
    path_or_url = attr.ib(
        type=str,
        default='',
        metadata=dict(help='Package directory path or URL'),
    )

    links = attr.ib(
        type=list,
        default=attr.Factory(list),
        metadata=dict(help='List of links available in this repo'),
    )

    def __attrs_post_init__(self):
        if not self.links:
            self.links = get_paths_or_urls(links_url=self.path_or_url)
        if not self.packages_by_normalized_name:
            for p in PypiPackage.packages_from_many_paths_or_urls(paths_or_urls=self.links):
                normalized_name = p.normalized_name
                self.packages_by_normalized_name[normalized_name].append(p)
                self.packages_by_normalized_name_version[(normalized_name, p.version)] = p

    def get_links(self, *args, **kwargs):
        return self.links or []

    def get_versions(self, name):
        name = name and NameVer.normalize_name(name)
        return self.packages_by_normalized_name.get(name, [])

    def get_latest_version(self, name):
        return PypiPackage.get_latest_version(name, self.get_versions(name))

    def get_package(self, name, version):
        return PypiPackage.get_name_version(name, version, self.get_versions(name))


@attr.attributes
class PypiRepository(Repository):
    """
    Represents the public PyPI simple index.
    It is populated lazily based on requested packages names
    """
    simple_url = attr.ib(
        type=str,
        default=PYPI_SIMPLE_URL,
        metadata=dict(help='Base PyPI simple URL for this index.'),
    )

    links_by_normalized_name = attr.ib(
        type=dict,
        default=attr.Factory(lambda: defaultdict(list)),
        metadata=dict(help='Mapping of {package name: [links]} available in this repo'),
    )

    def _fetch_links(self, name):
        name = name and NameVer.normalize_name(name)
        return find_pypi_links(name=name, simple_url=self.simple_url)

    def _populate_links_and_packages(self, name):
        name = name and NameVer.normalize_name(name)
        if name in self.links_by_normalized_name:
            return

        links = self._fetch_links(name)
        self.links_by_normalized_name[name] = links

        packages = list(PypiPackage.packages_from_many_paths_or_urls(paths_or_urls=links))
        self.packages_by_normalized_name[name] = packages

        for p in packages:
            name = name and NameVer.normalize_name(p.name)
            self.packages_by_normalized_name_version[(name, p.version)] = p

    def get_links(self, name, *args, **kwargs):
        name = name and NameVer.normalize_name(name)
        self._populate_links_and_packages(name)
        return  self.links_by_normalized_name.get(name, [])

    def get_versions(self, name):
        name = name and NameVer.normalize_name(name)
        self._populate_links_and_packages(name)
        return self.packages_by_normalized_name.get(name, [])

    def get_latest_version(self, name):
        return PypiPackage.get_latest_version(name, self.get_versions(name))

    def get_package(self, name, version):
        return PypiPackage.get_name_version(name, version, self.get_versions(name))

################################################################################
# Globals for remote repos to be lazily created and cached on first use for the
# life of the session together with some convenience functions.
################################################################################


def get_local_packages(directory=THIRDPARTY_DIR):
    """
    Return the list of all PypiPackage objects built from a local directory. Return
    an empty list if the package cannot be found.
    """
    return list(PypiPackage.packages_from_one_path_or_url(path_or_url=directory))


def get_local_package(name, version, directory=THIRDPARTY_DIR):
    """
    Return the list of all PypiPackage objects built from a local directory. Return
    an empty list if the package cannot be found.
    """
    packages = get_local_packages(directory)
    return PypiPackage.get_name_version(name, version, packages)


_REMOTE_REPO = None


def get_remote_repo(remote_links_url=REMOTE_LINKS_URL):
    global _REMOTE_REPO
    if not _REMOTE_REPO:
        _REMOTE_REPO = LinksRepository(path_or_url=remote_links_url)
    return _REMOTE_REPO


def get_remote_package(name, version, remote_links_url=REMOTE_LINKS_URL):
    return get_remote_repo(remote_links_url).get_package(name, version)


_PYPI_REPO = None


def get_pypi_repo(pypi_simple_url=PYPI_SIMPLE_URL):
    global _PYPI_REPO
    if not _PYPI_REPO:
        _PYPI_REPO = PypiRepository(simple_url=pypi_simple_url)
    return _PYPI_REPO


def get_pypi_package(name, version, pypi_simple_url=PYPI_SIMPLE_URL):
    return get_pypi_repo(pypi_simple_url).get_package(name, version)

################################################################################
#
# Basic file and URL-based operations using a persistent file-based Cache
#
################################################################################


@attr.attributes
class Cache:
    """
    A simple file-based cache based only on a filename presence.
    This is used to avoid impolite fetching from remote locations.
    """

    directory = attr.ib(type=str, default='.cache/thirdparty')

    def __attrs_post_init__(self):
        os.makedirs(self.directory, exist_ok=True)

    def clear(self):
        import shutil
        shutil.rmtree(self.directory)

    def get(self, path_or_url, as_text=True):
        """
        Get a file from a `path_or_url` through the cache.
        `path_or_url` can be a path or a URL to a file.
        """
        filename = os.path.basename(path_or_url.strip('/'))
        cached = os.path.join(self.directory, filename)

        if not os.path.exists(cached):
            print(f'Fetching {path_or_url}')
            content = get_file_content(path_or_url=path_or_url, as_text=as_text)
            wmode = 'w' if as_text else 'wb'
            with open(cached, wmode) as fo:
                fo.write(content)
            return content
        else:
            return get_local_file_content(path=cached, as_text=as_text)

    def put(self, filename, content):
        """
        Put in the cache the `content` of `filename`.
        """
        cached = os.path.join(self.directory, filename)
        wmode = 'wb' if isinstance(content, bytes) else 'w'
        with open(cached, wmode) as fo:
            fo.write(content)


def get_file_content(path_or_url, as_text=True):
    """
    Fetch and return the content at `path_or_url` from either a local path or a
    remote URL. Return the content as bytes is `as_text` is False.
    """
    if (path_or_url.startswith('file://')
        or (path_or_url.startswith('/') and os.path.exists(path_or_url))
    ):
        return get_local_file_content(path=path_or_url, as_text=as_text)

    elif path_or_url.startswith('https://'):
        return get_remote_file_content(url=path_or_url, as_text=as_text)

    else:
        raise Exception(f'Unsupported URL scheme: {path_or_url}')


def get_local_file_content(path, as_text=True):
    """
    Return the content at `url` as text. Return the content as bytes is
    `as_text` is False.
    """
    if path.startswith('file://'):
        path = path[7:]

    mode = 'r' if as_text else 'rb'
    with open(path, mode) as fo:
        return fo.read()


class RemoteNotFetchedException(Exception):
    pass


def get_remote_file_content(url, as_text=True, _delay=0):
    """
    Fetch and return the content at `url` as text. Return the content as bytes
    is `as_text` is False. Retries multiple times to fetch if there is a HTTP
    429 throttling response and this with an increasing delay.
    """
    time.sleep(_delay)
    response = requests.get(url)
    status = response.status_code
    if status != requests.codes.ok:  # NOQA
        if status == 429 and _delay < 20:
            # too many requests: start some exponential delay
            increased_delay = (_delay * 2) or 1
            return get_remote_file_content(url, as_text=True, _delay=increased_delay)
        else:
            raise RemoteNotFetchedException(f'Failed HTTP request from {url}: {status}' % locals())
    return response.text if as_text else response.content


def fetch_and_save_filename_from_paths_or_urls(
    filename,
    paths_or_urls,
    dest_dir=THIRDPARTY_DIR,
    as_text=True,
):
    """
    Return the content from fetching the `filename` file name found in the
    `paths_or_urls` list of URLs or paths and save to `dest_dir`. Raise an
    Exception on errors. Treats the content as text if `as_text` is True
    otherwise as binary.
    """
    path_or_url = get_link_for_filename(
        filename=filename,
        paths_or_urls=paths_or_urls,
    )

    return fetch_and_save_path_or_url(
        filename=filename,
        dest_dir=dest_dir,
        path_or_url=path_or_url,
        as_text=as_text,
    )


def fetch_content_from_path_or_url_through_cache(path_or_url, as_text=True, cache=Cache()):
    """
    Return the content from fetching at path or URL. Raise an Exception on
    errors. Treats the content as text if as_text is True otherwise as treat as
    binary. Use the provided file cache. This is the main entry for using the
    cache.

    Note: the `cache` argument is a global, though it does not really matter
    since it does not hold any state which is only kept on disk.
    """
    return cache.get(path_or_url=path_or_url, as_text=as_text)


def fetch_and_save_path_or_url(filename, dest_dir, path_or_url, as_text=True):
    """
    Return the content from fetching the `filename` file name at URL or path
    and save to `dest_dir`. Raise an Exception on errors. Treats the content as
    text if as_text is True otherwise as treat as binary.
    """
    content = fetch_content_from_path_or_url_through_cache(path_or_url, as_text)
    output = os.path.join(dest_dir, filename)
    wmode = 'w' if as_text else 'wb'
    with open(output, wmode) as fo:
        fo.write(content)
    return content

################################################################################
#
# Sync and fix local thirdparty directory for various issues and gaps
#
################################################################################


def add_missing_sources(dest_dir=THIRDPARTY_DIR):
    """
    Given a thirdparty dir, fetch missing source distributions from our remote
    repo or PyPI. Return a list of (name, version) tuples for source
    distribution that were not found
    """
    not_found = []
    local_packages = get_local_packages(directory=dest_dir)
    remote_repo = get_remote_repo()
    pypi_repo = get_pypi_repo()

    for package in local_packages:
        if not package.sdist:
            print()
            print(f'Finding sources for: {package.name} {package.version}')
            pypi_package = pypi_repo.get_package(
                name=package.name, version=package.version)

            print(f' --> Try fetching sources from Pypi: {pypi_package}')
            if pypi_package and pypi_package.sdist:
                print(f' --> Fetching sources from Pypi: {pypi_package.sdist}')
                pypi_package.fetch_sdist(dest_dir=dest_dir)
            else:
                remote_package = remote_repo.get_package(
                    name=package.name, version=package.version)

                print(f' --> Try fetching sources from remote: {pypi_package}')
                if remote_package and remote_package.sdist:
                    print(f' --> Fetching sources from Remote: {remote_package.sdist}')
                    remote_package.fetch_sdist(dest_dir=dest_dir)

                else:
                    print(f' --> no sources found')
                    not_found.append((package.name, package.version,))

    if not_found:
        for name, version in not_found:
            print(f'sdist not found for {name}=={version}')

    return not_found


def add_missing_about_files(dest_dir=THIRDPARTY_DIR):
    """
    Given a thirdparty dir, add missing ANOUT files and licenses using best efforts:
    - fetch from our mote links
    - derive from existing packages of the same name and version that would have such ABOUT file
    - derive from existing packages of the same name and different version that would have such ABOUT file
    - attempt to make API calls to fetch package details and create ABOUT file
    - create a skinny ABOUT file as a last resort
    """
    # first get available ones from our remote repo
    remote_repo = get_remote_repo()
    paths_or_urls = remote_repo.get_links()
    fetch_abouts(dest_dir=dest_dir, paths_or_urls=paths_or_urls)

    # then derive or create
    existing_about_files = set(get_about_files(dest_dir))
    local_packages = get_local_packages(directory=dest_dir)

    for package in local_packages:
        remote_package = remote_repo.get_package(package.name, package.version)
        for dist in package.get_distributions():
            filename = dist.filename
            download_url = dist.get_best_download_url()
            about_file = f'{filename}.ABOUT'
            if about_file not in existing_about_files:
                # TODO: also derive files from API calls
                about_file = create_or_derive_about_file(
                    remote_package=remote_package,
                    name=package.normalized_name,
                    version=package.version,
                    filename=filename,
                    download_url=download_url,
                    dest_dir=dest_dir,
                )
            if not about_file:
                print(f'Unable to derive/create/fetch ABOUT file: {about_file}')


def fix_about_files_checksums(dest_dir=THIRDPARTY_DIR):
    """
    Given a thirdparty dir, fix ABOUT files checksums
    """
    for about_file in get_about_files(dest_dir):
        about_loc = os.path.join(dest_dir, about_file)
        resource_loc = about_loc.replace('.ABOUT', '')
        with open(about_loc) as fi:
            about = saneyaml.load(fi.read())

        checksums = multi_checksums(resource_loc, checksum_names=('md5', 'sha1',))
        for k, v in checksums.items():
            about[f'checksum_{k}'] = v

        with open(about_loc, 'w') as fo:
            fo.write(saneyaml.dump(about))


def fetch_missing_wheels(dest_dir=THIRDPARTY_DIR):
    """
    Given a thirdparty dir fetch missing wheels for all known combos of Python
    versions and OS. Return a list of tuple (Package, Environmentt) for wheels
    that were not found locally or remotely.
    """
    local_packages = get_local_packages(directory=dest_dir)
    return fetch_wheels_for_packages(local_packages, dest_dir=dest_dir)


def fetch_wheels_for_packages(
    packages,
    python_versions=PYTHON_VERSIONS,
    operating_systems=PLATFORMS_BY_OS,
    dest_dir=THIRDPARTY_DIR,
):
    """
    Given a thirdparty dir fetch missing wheels for all known combos of Python
    versions and OS for a `packages` list of Package. Return a list of tuple
    (Package, Environment) for wheels that were not found locally or remotely.
    """

    evts = itertools.product(python_versions, operating_systems)
    environments = [Environment.from_pyver_and_os(pyv, os) for pyv, os in evts]
    packages_and_envts = itertools.product(packages, environments)
    return fetch_wheels_for_packages_and_envts(
        packages_and_envts=packages_and_envts, dest_dir=dest_dir)


def fetch_wheels_for_packages_and_envts(
    packages_and_envts,
    dest_dir=THIRDPARTY_DIR,
):
    """
    Given a thirdparty dir fetch missing wheels for a `packages_and_envts` list
    of (Package, Environment) tuples. Return a list of tuple
    (Package, Environment) for wheels that were not found locally or remotely.
    """

    missing_to_build = []
    for package, envt in packages_and_envts:
        filename = package.fetch_wheel(environment=envt, dest_dir=dest_dir)
        if not filename:
            missing_to_build.append((package, envt))
            print(
                f'Wheel not found for {package.name}=={package.version} '
                f'on {envt.operating_system} for Python {envt.python_version}')

    return missing_to_build


def build_missing_wheels(
    packages_and_envts,
    build_remotely=False,
    dest_dir=THIRDPARTY_DIR,
):
    """
    Build all wheels in a list of tuple (Package, Environmentt) and save in
    `dest_dir`. Return a list of tuple (Package, Environment), and a list of
    built wheel filenames.
    """

    not_built = []
    built_filenames = []

    packages_and_envts = itertools.groupby(
        sorted(packages_and_envts), key=operator.itemgetter(0))

    for package, pkg_envts in packages_and_envts:

        envts = [envt for _pkg, envt in pkg_envts]
        python_dot_versions = sorted(set(e.python_dot_version for e in envts))
        operating_systems = sorted(set(e.operating_system for e in envts))
        built = None
        try:
            built = build_wheels(
                requirements_specifier=package.specifier,
                with_deps=False,
                build_remotely=build_remotely,
                python_dot_versions=python_dot_versions,
                operating_systems=operating_systems,
                verbose=False,
                dest_dir=dest_dir,
            )
        except Exception as e:
            import traceback
            print('#############################################################')
            print('#############################################################')
            print('#############     WHEEL BUILD FAILED   ######################')
            print('#############################################################')
            traceback.print_exc()
            print()
            print('#############################################################')
            print('#############################################################')

        if not built:
            for envt in pkg_envts:
                not_built.append((package, envt))
        else:
            for bfn in built:
                print(f'   --> Built wheel: {bfn}')
                built_filenames.append(bfn)

    return not_built, built_filenames


def add_missing_licenses_and_notices(dest_dir=THIRDPARTY_DIR):
    """
    Given a thirdparty dir that is assumed to be in sync with the
    REMOTE_FIND_LINKS repo, fetch missing license and notice files.
    """

    not_found = []

    # fetch any remote ones, then from licensedb
    paths_or_urls = get_paths_or_urls(links_url=REMOTE_BASE_DOWNLOAD_URL)
    errors = fetch_license_texts_and_notices(dest_dir, paths_or_urls)
    not_found.extend(errors)

    errors = fetch_license_texts_from_licensedb(dest_dir)
    not_found.extend(errors)

    # TODO: also make API calls

    for name, version, pyver, opsys in not_found:
        print(f'Not found wheel for {name}=={version} on python {pyver} and {opsys}')


def delete_outdated_package_files(dest_dir=THIRDPARTY_DIR):
    """
    Keep only the latest version of any PypiPackage found in `dest_dir`.
    Delete wheels, sdists and ABOUT files for older versions.
    """
    local_packages = get_local_packages(directory=dest_dir)
    key = operator.attrgetter('name')
    package_versions_by_name = itertools.groupby(local_packages, key=key)
    for name, package_versions in package_versions_by_name:
        for outdated in PypiPackage.get_outdated_versions(name, package_versions):
            outdated.delete_files(dest_dir)


def delete_unused_license_and_notice_files(dest_dir=THIRDPARTY_DIR):
    """
    Using .ABOUT files found in `dest_dir` remove any license file found in
    `dest_dir` that is not referenced in any .ABOUT file.
    """
    referenced_license_files = set()

    license_files = set([f for f in os.listdir(dest_dir) if f.endswith('.LICENSE')])

    for about_data in get_about_datas(dest_dir):
        lfns = get_license_and_notice_filenames(about_data)
        referenced_license_files.update(lfns)

    unused_license_files = license_files.difference(referenced_license_files)
    for unused in unused_license_files:
        fileutils.delete(os.path.join(dest_dir, unused))

################################################################################
#
# Functions to handle remote or local repo used to "find-links"
#
################################################################################


def get_paths_or_urls(links_url):
    if links_url.startswith('https:'):
        paths_or_urls = find_links_from_url(links_url)
    else:
        paths_or_urls = find_links_from_dir(links_url)
    return paths_or_urls


def find_links_from_dir(directory=THIRDPARTY_DIR, extensions=EXTENSIONS):
    """
    Return a list of path to files in `directory` for any file that ends with
    any of the extension in the list of `extensions` strings.
    """
    base = os.path.abspath(directory)
    files = [os.path.join(base, f) for f in os.listdir(base) if f.endswith(extensions)]
    return files


def find_links_from_url(
    links_url=REMOTE_LINKS_URL,
    base_url=REMOTE_BASE_URL,
    prefix=REMOTE_HREF_PREFIX,
    extensions=EXTENSIONS,
):
    """
    Return a list of download link URLs found in the HTML page at `links_url`
    URL that starts with the `prefix` string and ends with any of the extension
    in the list of `extensions` strings. Use the `base_url` to prefix the links.
    """
    get_links = re.compile('href="([^"]+)"').findall

    text = get_remote_file_content(links_url)
    links = get_links(text)
    links = [l for l in links if l.startswith(prefix) and l.endswith(extensions)]
    links = [l if l.startswith('https://') else f'{base_url}{l}' for l in links]
    return links


def find_pypi_links(name, extensions=EXTENSIONS, simple_url=PYPI_SIMPLE_URL):
    """
    Return a list of download link URLs found in a PyPI simple index for package name.
    with the list of `extensions` strings. Use the `simple_url` PyPI url.
    """
    get_links = re.compile('href="([^"]+)"').findall

    name = name and NameVer.normalize_name(name)
    simple_url = simple_url.strip('/')
    simple_url = f'{simple_url}/{name}'

    text = get_remote_file_content(simple_url)
    links = get_links(text)
    links = [l.partition('#sha256=') for l in links]
    links = [url for url, _, _sha256 in links]
    links = [l for l in links if l.endswith(extensions)]
    return  links


def get_link_for_filename(filename, paths_or_urls):
    """
    Return a link for `filename` found in the `links` list of URLs or paths. Raise an
    exception if no link is found or if there are more than one link for that
    file name.
    """
    path_or_url = [l for l in paths_or_urls if l.endswith(f'/{filename}')]
    if not path_or_url:
        raise Exception(f'Missing link to file: {filename}')
    if not len(path_or_url) == 1:
        raise Exception(f'Multiple links to file: {filename}: \n' + '\n'.join(path_or_url))
    return path_or_url[0]

################################################################################
#
# Requirements processing
#
################################################################################


class MissingRequirementException(Exception):
    pass


def get_required_packages(required_name_versions):
    """
    Return a tuple of (remote packages, PyPI packages) where each is a mapping
    of {(name, version): PypiPackage}  for packages listed in the
    `required_name_versions` list of (name, version) tuples. Raise a
    MissingRequirementException with a list of missing (name, version) if a
    requirement cannot be satisfied remotely or in PyPI.
    """
    remote_repo = get_remote_repo()

    remote_packages = {(name, version): remote_repo.get_package(name, version)
        for name, version in required_name_versions}

    pypi_repo = get_pypi_repo()
    pypi_packages = {(name, version):  pypi_repo.get_package(name, version)
        for name, version in required_name_versions}

    # remove any empty package (e.g. that do not exist in some place)
    remote_packages = {nv: p for nv, p in remote_packages.items() if p}
    pypi_packages = {nv: p for nv, p in pypi_packages.items() if p}

    # check that we are not missing any
    repos_name_versions = set(remote_packages.keys()) | set(pypi_packages.keys())
    missing_name_versions = required_name_versions.difference(repos_name_versions)
    if missing_name_versions:
        raise MissingRequirementException(sorted(missing_name_versions))

    return remote_packages, pypi_packages


def get_required_remote_packages(requirements_file='requirements.txt'):
    """
    Yield tuple of (name, version, PypiPackage) for packages listed in the
    `requirements_file` requirements file and found in our remote repo exclusively.
    """
    required_name_versions = load_requirements(requirements_file=requirements_file)
    remote_repo = get_remote_repo()
    return (
        (name, version, remote_repo.get_package(name, version))
        for name, version in required_name_versions
    )


def update_requirements(name, version=None, requirements_file='requirements.txt'):
    """
    Upgrade or add `package_name` with `new_version` to the `requirements_file`
    requirements file. Write back requirements sorted with name and version
    canonicalized. Note: this cannot deal with hashed or unpinned requirements.
    Do nothng if the version already exists as pinned.
    """
    normalized_name = NameVer.normalize_name(name)

    is_updated = False
    updated_name_versions = []
    for existing_name, existing_version in load_requirements(requirements_file, force_pinned=False):
        existing_normalized_name = NameVer.normalize_name(existing_name)

        if normalized_name == existing_normalized_name:
            if version != existing_version:
                is_updated = True
            updated_name_versions.append((existing_normalized_name, existing_version,))

    if is_updated:
        updated_name_versions = sorted(updated_name_versions)
        nvs = '\n'.join(f'{name}=={version}' for name, version in updated_name_versions)

        with open(requirements_file, 'w') as fo:
            fo.write(nvs)

################################################################################
#
# ABOUT and license files functions
#
################################################################################


def get_about_files(dest_dir=THIRDPARTY_DIR):
    """
    Return a list of ABOUT files found in `dest_dir`
    """
    return [f for f in os.listdir(dest_dir) if f.endswith('.ABOUT')]


def get_about_datas(dest_dir=THIRDPARTY_DIR):
    """
    Yield ABOUT data mappings from ABOUT files found in `dest_dir`
    """
    for about_file in get_about_files(dest_dir):
        with open(os.path.join(dest_dir, about_file)) as fi:
            yield saneyaml.load(fi.read())


def create_or_derive_about_file(
    remote_package,
    name,
    version,
    filename,
    download_url,
    dest_dir=THIRDPARTY_DIR,
):
    """
    Derive an ABOUT file from an existing remote package if possible. Otherwise,
    create a skinny ABOUT file using the provided name, version filename and
    download_url. Return filename on success or None.
    """

    about_file = None

    if remote_package:
        for dist in remote_package.get_distributions():
            about_filename = derive_about_file_from_dist(
                dist=dist,
                name=name,
                version=version,
                filename=filename,
                download_url=download_url,
                dest_dir=dest_dir,
            )
            if about_filename:
                return about_filename

    if not about_file:
        # Create and save a skinny ABOUT file with minimum known data.
        normalized_name = NameVer.normalize_name(name)

        about_data = dict(
            about_resource=filename,
            name=normalized_name,
            version=version,
            download_url=download_url,
            primary_language='Python',
        )
        about_file = f'{filename}.ABOUT'
        with open(os.path.join(dest_dir, about_file), 'w') as fo:
            fo.write(saneyaml.dump(about_data))

    if not about_file:
        raise Exception(
            f'Failed to create an ABOUT file for: {filename}')

    return about_file


def derive_about_file_from_dist(
    dist,
    name,
    version,
    filename,
    download_url=None,
    dest_dir=THIRDPARTY_DIR,
):
    """
    Derive and save a new ABOUT file from dist for the provided argument.
    Return the ABOUT file name on success, None otherwise.
    """

    try:
        dist_about_text = fetch_content_from_path_or_url_through_cache(dist.about_download_url)
    except RemoteNotFetchedException:
        return

    if not dist_about_text:
        return

    if not download_url:
        # binary has been built from sources, therefore this is NOT from PyPI
        # so we raft a wheel URL assuming this will be later uploaded
        # to our PyPI-like repo
        download_url = Distribution.build_remote_download_url(filename)

    about_filename = derive_new_about_file_from_about_text(
        about_text=dist_about_text,
        new_name=name,
        new_version=version,
        new_filename=filename,
        new_download_url=download_url,
        dest_dir=dest_dir,
    )

    # also fetch and rename the NOTICE
    dist_about_data = saneyaml.load(dist_about_text)
    existing_notice_filename = dist_about_data.get('notice_file')

    if existing_notice_filename:
        existing_notice_file_loc = os.path.join(dest_dir, existing_notice_filename)

        derived_notice_filename = f'{filename}.NOTICE'
        derived_notice_file_loc = os.path.join(dest_dir, derived_notice_filename)

        if os.path.exists(existing_notice_file_loc):
            fileutils.copyfile(existing_notice_file_loc, derived_notice_file_loc)
        else:
            existing_notice_url = Distribution.build_remote_download_url(existing_notice_filename)
            fetch_and_save_path_or_url(
                filename=derived_notice_filename,
                dest_dir=dest_dir,
                path_or_url=existing_notice_url,
                as_text=True,
            )

    return about_filename


def derive_new_about_file_from_about_file(
    existing_about_file,
    new_name,
    new_version,
    new_filename,
    new_download_url,
    dest_dir=THIRDPARTY_DIR,
):
    """
    Given an existing ABOUT file `existing_about_file` in `dest_dir`, create a
    new ABOUT file derived from that existing file and save it to `dest_dir`.
    Use new_name, new_version, new_filename, new_download_url for the new ABOUT
    file.
    """
    with open(existing_about_file) as fi:
        about_text = fi.read()

    return derive_new_about_file_from_about_text(
        about_text=about_text,
        new_name=new_name,
        new_version=new_version,
        new_filename=new_filename,
        new_download_url=new_download_url,
        dest_dir=dest_dir,
    )


def derive_new_about_file_from_about_text(
    about_text,
    new_name,
    new_version,
    new_filename,
    new_download_url,
    dest_dir=THIRDPARTY_DIR,
):
    """
    Given an existing ABOUT YAML text `about_text` , create a
    new .ABOUT file derived from that existing content and save it to `dest_dir`.
    Use new_name, new_version, new_filename, new_download_url for the new ABOUT
    file. Return the new ABOUT file name
    """

    normalized_new_name = NameVer.normalize_name(new_name)

    about_data = saneyaml.load(about_text)
    # remove checksums if any
    for checksum in ('checksum_md5', 'checksum_sha1', 'checksum_sha256', 'checksum_sha512'):
        about_data.pop(checksum, None)

    about_data['about_resource'] = new_filename
    about_data['name'] = normalized_new_name
    about_data['version'] = new_version
    about_data['download_url'] = new_download_url

    new_about_text = saneyaml.dump(about_data)

    new_about_filename = os.path.join(dest_dir, f'{new_filename}.ABOUT')

    with open(new_about_filename, 'w') as fo:
        fo.write(new_about_text)

    return new_about_filename


def fetch_license_texts_and_notices(dest_dir, paths_or_urls):
    """
    Download to `dest_dir` all the .LICENSE and .NOTICE files referenced in all
    the .ABOUT files in `dest_dir` using URLs or path from the `paths_or_urls`
    list.
    """
    errors = []
    for about_data in get_about_datas(dest_dir):
        for lic_file in get_license_and_notice_filenames(about_data):
            try:

                lic_url = get_link_for_filename(
                    filename=lic_file,
                    paths_or_urls=paths_or_urls,
                )

                fetch_and_save_path_or_url(
                    filename=lic_file,
                    dest_dir=dest_dir,
                    path_or_url=lic_url,
                    as_text=True,
                )
            except Exception as e:
                errors.append(str(e))
                continue

    return errors


def fetch_license_texts_from_licensedb(
    dest_dir=THIRDPARTY_DIR,
    licensedb_api_url=LICENSEDB_API_URL,
):
    """
    Download to `dest_dir` all the .LICENSE files referenced in all the .ABOUT
    files in `dest_dir` using the licensedb `licensedb_api_url`.
    """
    errors = []
    for about_data in get_about_datas(dest_dir):
        for license_key in get_license_keys(about_data):
            ltext = fetch_and_save_license_text_from_licensedb(
                license_key,
                dest_dir,
                licensedb_api_url,
            )
            if not ltext:
                errors.append(f'No text for license {license_key}')

    return errors


def fetch_abouts(dest_dir, paths_or_urls):
    """
    Download to `dest_dir` all the .ABOUT files for all the files in `dest_dir`
    that should have an .ABOUT file documentation using URLs or path from the
    `paths_or_urls` list.

    Documentable files (typically archives, sdists, wheels, etc.) should have a
    corresponding .ABOUT file named <archive_filename>.ABOUT.
    """

    # these are the files that should have a matching ABOUT file
    aboutables = [fn for fn in os.listdir(dest_dir)
        if not fn.endswith(EXTENSIONS_ABOUT)
    ]

    errors = []
    for aboutable in aboutables:
        about_file = f'{aboutable}.ABOUT'
        try:
            about_url = get_link_for_filename(
                filename=about_file,
                paths_or_urls=paths_or_urls,
            )

            fetch_and_save_path_or_url(
                filename=about_file,
                dest_dir=dest_dir,
                path_or_url=about_url,
                as_text=True,
            )

        except Exception as e:
            errors.append(str(e))

    return errors


def get_license_keys(about_data):
    """
    Return a list of license key found in the `about_data` .ABOUT data
    mapping.
    """
    # collect all the license and notice files
    # - first explicitly listed as licenses keys
    licenses = about_data.get('licenses', [])
    keys = [l.get('key') for l in licenses]
    # - then implied key from the license expression
    license_expression = about_data.get('license_expression', '')
    keys += keys_from_expression(license_expression)
    keys = [l for l in keys if l]
    return sorted(set(keys))


def get_license_filenames(about_data):
    """
    Return a list of license file names found in the `about_data` .ABOUT data
    mapping.
    """
    return [f'{l}.LICENSE' for l in get_license_keys(about_data)]


def get_notice_filename(about_data):
    """
    Yield the notice file name found in the `about_data` .ABOUT data
    mapping.
    """
    notice_file = about_data.get('notice_file')
    if notice_file:
        yield notice_file


def get_license_and_notice_filenames(about_data):
    """
    Return a list of license file names found in the `about_data` .ABOUT data
    mapping.
    """

    license_files = get_license_filenames(about_data)

    licenses = about_data.get('licenses', [])
    license_files += [l.get('file') for l in licenses]

    license_files += list(get_notice_filename(about_data))
    return sorted(set(f for f in license_files if f))


def keys_from_expression(license_expression):
    """
    Return a list of license keys from a `license_expression` string.
    """
    cleaned = (license_expression
        .lower()
        .replace('(', ' ')
        .replace(')', ' ')
        .replace(' and ', ' ')
        .replace(' or ', ' ')
        .replace(' with ', ' ')
    )
    return cleaned.split()


def fetch_and_save_license_text_from_licensedb(
    license_key,
    dest_dir=THIRDPARTY_DIR,
    licensedb_api_url=LICENSEDB_API_URL,
):
    """
    Fetch and save the license text for `license_key` from the `licensedb_api_url`
    """
    filename = f'{license_key}.LICENSE'
    api_url = f'{licensedb_api_url}/{filename}'
    return fetch_and_save_path_or_url(filename, dest_dir, path_or_url=api_url, as_text=True)

################################################################################
#
# pip-based functions running pip as if called from the command line
#
################################################################################


def call(args):
    """
    Call args in a subprocess and display output on the fly.
    Return or raise stdout, stderr, returncode
    """
    print('Calling:', ' '.join(args))
    with subprocess.Popen(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        encoding='utf-8'
    ) as process:

        while True:
            line = process.stdout.readline()
            if not line and process.poll() is not None:
                break
            print(line.rstrip(), flush=True)

        stdout, stderr = process.communicate()
        returncode = process.returncode
        if returncode == 0:
            return stdout, stderr, returncode
        else:
            raise Exception(stdout, stderr, returncode)


def fetch_wheels_using_pip(
        environment=None,
        requirements_file='requirements.txt',
        dest_dir=THIRDPARTY_DIR,
        links_url=REMOTE_LINKS_URL,
):
    """
    Download all dependent wheels for the `environment` Enviromnent constraints
    in the `requirements_file` requirements file or package requirement into
    `dest_dir` directory.

    Use only the packages found in the `links_url` HTML page ignoring PyPI
    packages unless `links_url` is None or empty in which case we use instead
    the public PyPI packages.

    If the provided `environment` is None then the current Python interpreter
    environment is used implicitly.
    """

    options = [
        'pip', 'download',
        '--requirement', requirements_file,
        '--dest', dest_dir,
        '--only-binary=:all:',
        '--no-deps',
    ]

    if links_url:
        find_link = [
            '--no-index',
            '--find-links', links_url,
        ]
        options += find_link

    if environment:
        options += environment.get_pip_cli_options()

    try:
        call(options)
    except:
        print('Failed to run:')
        print(' '.join(options))
        raise


def fetch_sources_using_pip(
        requirements_file='requirements.txt',
        dest_dir=THIRDPARTY_DIR,
        links_url=REMOTE_LINKS_URL,
):
    """
    Download all dependency source distributions for the `environment`
    Enviromnent constraints in the `requirements_file` requirements file or
    package requirement into `dest_dir` directory.

    Use only the source packages found in the `links_url` HTML page ignoring
    PyPI packages unless `links_url` is None or empty in which case we use
    instead the public PyPI packages.

    These items are fetched:
        - source distributions
    """

    options = [
        'pip', 'download',
        '--requirement', requirements_file,
        '--dest', dest_dir,
        '--no-binary=:all:'
        '--no-deps',
    ] + [
        # temporary workaround
        '--only-binary=extractcode-7z',
        '--only-binary=extractcode-libarchive',
        '--only-binary=typecode-libmagic',
    ]

    if links_url:
        options += [
            '--no-index',
            '--find-links', links_url,
        ]

    try:
        call(options)
    except:
        print('Failed to run:')
        print(' '.join(options))
        raise

################################################################################
# Utility to build new Python wheel.
################################################################################


def build_wheels(
    requirements_specifier,
    with_deps=False,
    build_remotely=False,
    python_dot_versions=PYTHON_DOT_VERSIONS,
    operating_systems=PLATFORMS_BY_OS,
    verbose=False,
    dest_dir=THIRDPARTY_DIR,
):
    """
    Given a pip `requirements_specifier` string (such as package names or as
    name==version), build the corresponding binary wheel(s) for all
    `python_dot_versions` and `operating_systems` combinations and save them
    back in `dest_dir` and return a list of built wheel file names.

    Include wheels for all dependencies if `with_deps` is True.

    First try to build locally to process pure Python wheels, and fall back to
    build remotey on all requested Pythons and operating systems.
    """
    locally_built = build_wheels_locally_if_pure_python(
        requirements_specifier=requirements_specifier,
        with_deps=with_deps,
        verbose=verbose,
        dest_dir=dest_dir,
    )

    if locally_built:
        return locally_built

    if build_remotely:
        return build_wheels_remotely_on_multiple_platforms(
            requirements_specifier=requirements_specifier,
            with_deps=with_deps,
            python_dot_versions=python_dot_versions,
            operating_systems=operating_systems,
            verbose=verbose,
            dest_dir=dest_dir,
        )
    else:
        return []


def build_wheels_remotely_on_multiple_platforms(
    requirements_specifier,
    with_deps=False,
    python_dot_versions=PYTHON_DOT_VERSIONS,
    operating_systems=PLATFORMS_BY_OS,
    verbose=False,
    dest_dir=THIRDPARTY_DIR,
):
    """
    Given pip `requirements_specifier` string (such as package names or as
    name==version), build the corresponding binary wheel(s) including wheels for
    all dependencies for all `python_dot_versions` and `operating_systems`
    combinations and save them back in `dest_dir` and return a list of built
    wheel file names.
    """
    # these environment variable must be set before
    has_envt = (
        os.environ.get('ROMP_BUILD_REQUEST_URL') and
        os.environ.get('ROMP_DEFINITION_ID') and
        os.environ.get('ROMP_PERSONAL_ACCESS_TOKEN') and
        os.environ.get('ROMP_USERNAME')
    )

    if not has_envt:
        raise Exception(
            'ROMP_BUILD_REQUEST_URL, ROMP_DEFINITION_ID, '
            'ROMP_PERSONAL_ACCESS_TOKEN and ROMP_USERNAME '
            'are required enironment variables.')

    python_cli_options = list(itertools.chain.from_iterable(
        ('--version', ver) for ver in python_dot_versions))

    os_cli_options = list(itertools.chain.from_iterable(
        ('--platform' , plat) for plat in operating_systems))

    deps = '' if with_deps else '--no-deps'
    verbose = '--verbose' if verbose else ''

    romp_args = ([
        'romp',
        '--interpreter', 'cpython',
        '--architecture', 'x86_64',
        '--check-period', '5',  # in seconds

    ] + python_cli_options + os_cli_options + [

        '--artifact-paths', '*.whl',
        '--artifact', 'artifacts.tar.gz',
        '--command',
            # create a virtualenv, upgrade pip
#            f'python -m ensurepip --user --upgrade; '
            f'python -m pip {verbose} install  --user --upgrade pip setuptools wheel; '
            f'python -m pip {verbose} wheel {deps} {requirements_specifier}',
    ])

    if verbose:
        romp_args.append('--verbose')

    print(f'Building wheels for: {requirements_specifier}')
    print(f'Using command:', ' '.join(romp_args))
    call(romp_args)

    wheel_filenames = extract_tar('artifacts.tar.gz', dest_dir)
    for wfn in wheel_filenames:
        print(f' built wheel: {wfn}')
    return wheel_filenames


def build_wheels_locally_if_pure_python(
    requirements_specifier,
    with_deps=False,
    verbose=False,
    dest_dir=THIRDPARTY_DIR,
):
    """
    Given pip `requirements_specifier` string (such as package names or as
    name==version), build the corresponding binary wheel(s) locally.

    If all these are "pure" Python wheels that run on all Python 3 versions and
    operating systems, copy them back in `dest_dir` if they do not exists there
    and return a list of built wheel file names.

    Otherwise, if any is not pure, do nothing and return an empty list.
    """
    deps = [] if with_deps else ['--no-deps']
    verbose = ['--verbose'] if verbose else []

    wheel_dir = tempfile.mkdtemp(prefix='scancode-release-wheels-local-')
    cli_args = [
        'pip', 'wheel',
        '--wheel-dir', wheel_dir,
    ] + deps + verbose + [
        requirements_specifier
    ]

    print(f'Building local wheels for: {requirements_specifier}')
    print(f'Using command:', ' '.join(cli_args))
    call(cli_args)

    built = os.listdir(wheel_dir)
    if not built:
        return []

    if not all(is_pure_wheel(bwfn) for bwfn in built):
        return []

    pure_built = []
    for bwfn in built:
        owfn = os.path.join(dest_dir, bwfn)
        if not os.path.exists(owfn):
            nwfn = os.path.join(wheel_dir, bwfn)
            fileutils.copyfile(nwfn, owfn)
        pure_built.append(bwfn)
        print(f'Built local wheel: {bwfn}')
    return pure_built


def optimize_wheel(wheel_filename, dest_dir=THIRDPARTY_DIR):
    """
    Optimize a wheel named `wheel_filename` in `dest_dir` such as renaming its
    tags for PyPI compatibility and making it smaller if possible. Return the
    name of the new wheel if renamed or the existing new name otherwise.
    """
    if is_pure_wheel(wheel_filename):
        print(f'Pure wheel: {wheel_filename}, nothing to do.')
        return wheel_filename

    original_wheel_loc = os.path.join(dest_dir, wheel_filename)
    wheel_dir = tempfile.mkdtemp(prefix='scancode-release-wheels-')
    awargs = [
        'auditwheel',
        'addtag',
        '--wheel-dir', wheel_dir,
       original_wheel_loc
    ]
    call(awargs)

    audited = os.listdir(wheel_dir)
    if not audited:
        # cannot optimize wheel
        return wheel_filename

    assert len(audited) == 1
    new_wheel_name = audited[0]

    new_wheel_loc = os.path.join(wheel_dir, new_wheel_name)

    # this needs to go now
    os.remove(original_wheel_loc)

    if new_wheel_name == wheel_filename:
        os.rename(new_wheel_loc, original_wheel_loc)
        return wheel_filename

    new_wheel = Wheel.from_filename(new_wheel_name)
    non_pypi_plats = utils_pypi_supported_tags.validate_platforms_for_pypi(new_wheel.platforms)
    new_wheel.platforms = [p for p in new_wheel.platforms if p not in non_pypi_plats]
    if not new_wheel.platforms:
        print(f'Cannot make wheel PyPI compatible: {original_wheel_loc}')
        os.rename(new_wheel_loc, original_wheel_loc)
        return wheel_filename

    new_wheel_cleaned_filename = new_wheel.to_filename()
    new_wheel_cleaned_loc = os.path.join(dest_dir, new_wheel_cleaned_filename)
    os.rename(new_wheel_loc, new_wheel_cleaned_loc)
    return new_wheel_cleaned_filename


def extract_tar(location, dest_dir=THIRDPARTY_DIR,):
    """
    Extract a tar archive at `location` in the `dest_dir` directory. Return a
    list of extracted locations (either directories or files).
    """
    with open(location, 'rb') as fi:
        with tarfile.open(fileobj=fi) as tar:
            members = list(tar.getmembers())
            tar.extractall(dest_dir, members=members)

    return [os.path.basename(ti.name) for ti in members
            if ti.type == tarfile.REGTYPE]


def add_or_upgrade_package(
        name,
        version=None,
        python_version=None,
        operating_system=None,
        dest_dir=THIRDPARTY_DIR,
    ):
    """
    Add or update package `name` and `version` as a binary wheel saved in
    `dest_dir`. Use the latest version if `version` is None. Return the a list
    of the built wheel file names or an empty list.

    Use the provided `python_version` (e.g. "36") and `operating_system` (e.g.
    linux, windows or macos) to decide which specific wheel to fetch or build.
    """
    environment = Environment.from_pyver_and_os(python_version, operating_system)

    # Check if requested wheel already exists locally for this version
    local_package = get_local_package(name, version)
    if version and local_package:
        for wheel in local_package.get_supported_wheels(environment):
            # if requested version is there, there is nothing to do: just return
            return [wheel.filename]

    if not version:
        # find latest version @ PyPI
        pypi_package = get_pypi_repo().get_latest_version(name)
        version = pypi_package.version

    # Check if requested wheel already exists remotely or in Pypi for this version
    wheel_filename = fetch_package_wheel(name, version, environment, dest_dir)
    if wheel_filename:
        return [wheel_filename]

    # the wheel is not available locally, remotely or in Pypi
    # we need to build binary from sources
    requirements_specifier = f'{name}=={version}'

    wheel_filenames = build_wheels(
        requirements_specifier=requirements_specifier,
        python_version=python_version,
        operating_system=operating_system,
        dest_dir=dest_dir,
    )

    return wheel_filenames