# Copyright (C) 2013  ABRT Team
# Copyright (C) 2013  Red Hat, Inc.
#
# This file is part of faf.
#
# faf is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# faf is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with faf.  If not, see <http://www.gnu.org/licenses/>.
from __future__ import absolute_import

import re

from typing import Dict, List

from pyfaf.opsys import System
from pyfaf.checker import DictChecker, IntChecker, ListChecker, StringChecker
from pyfaf.common import FafError, log
from pyfaf.queries import (get_arch_by_name,
                           get_opsys_by_name,
                           get_package_by_nevra,
                           get_reportpackage,
                           get_unknown_package)
from pyfaf.storage import (Arch,
                           Build,
                           OpSys,
                           Package,
                           ReportPackage,
                           ReportUnknownPackage,
                           column_len)
from pyfaf.repos import repo_types
from pyfaf.utils.parse import str2bool, words2list
from pyfaf.storage.custom_types import to_semver

__all__ = ["CentOS"]

# see https://github.com/abrt/faf/issues/695
# pylint: disable=abstract-method

class CentOS(System):
    name = "centos"
    nice_name = "CentOS"

    packages_checker = ListChecker(
        DictChecker({
            "name":            StringChecker(pattern=r"^[a-zA-Z0-9_\-\.\+~]+$",
                                             maxlen=column_len(Package,
                                                               "name")),
            "epoch":           IntChecker(minval=0),
            "version":         StringChecker(pattern=r"^[a-zA-Z0-9_\.\+~]+$",
                                             maxlen=column_len(Build, "version")),
            "release":         StringChecker(pattern=r"^[a-zA-Z0-9_\.\+]+$",
                                             maxlen=column_len(Build, "release")),
            "architecture":    StringChecker(pattern=r"^[a-zA-Z0-9_]+$",
                                             maxlen=column_len(Arch, "name")),
        }), minlen=0
    )

    ureport_checker = DictChecker({
        # no need to check name, version and architecture twice
        # the toplevel checker already did it
        # "name": StringChecker(allowed=[CentOS.name])
        # "version":        StringChecker()
        # "architecture":   StringChecker()
    })

    pkg_roles = ["affected", "related", "selinux_policy"]

    @classmethod
    def install(cls, db, logger=None) -> None:
        if logger is None:
            logger = log.getChild(cls.__name__)

        logger.info("Adding CentOS")
        new = OpSys()
        new.name = cls.nice_name
        db.session.add(new)
        db.session.flush()

    @classmethod
    def installed(cls, db) -> bool:
        return bool(get_opsys_by_name(db, cls.nice_name))

    def __init__(self) -> None:
        super().__init__()
        self.eol = None
        self.repo_urls = []
        self.allow_unpackaged = None
        self.inactive_releases = None
        self.active_releases = None
        self.load_config_to_self("repo_urls", ["centos.repo-urls"], [],
                                 callback=words2list)
        self.load_config_to_self("allow_unpackaged",
                                 ["ureport.allow-unpackaged"], False,
                                 callback=str2bool)
        self.load_config_to_self("inactive_releases", ["centos.inactive-releases"])
        self.load_config_to_self("active_releases", ["centos.active-releases"])

    def _save_packages(self, db, db_report, packages, count=1) -> None:
        for package in packages:
            role = "RELATED"
            if "package_role" in package:
                if package["package_role"] == "affected":
                    role = "CRASHED"
                elif package["package_role"] == "selinux_policy":
                    role = "SELINUX_POLICY"

            db_package = get_package_by_nevra(db,
                                              name=package["name"],
                                              epoch=package["epoch"],
                                              version=package["version"],
                                              release=package["release"],
                                              arch=package["architecture"])
            if db_package is None:
                self.log_warn("Package {0}-{1}:{2}-{3}.{4} not found in "
                              "storage".format(package["name"],
                                               package["epoch"],
                                               package["version"],
                                               package["release"],
                                               package["architecture"]))

                db_unknown_pkg = get_unknown_package(db,
                                                     db_report,
                                                     role,
                                                     package["name"],
                                                     package["epoch"],
                                                     package["version"],
                                                     package["release"],
                                                     package["architecture"])
                if db_unknown_pkg is None:
                    db_arch = get_arch_by_name(db, package["architecture"])
                    if db_arch is None:
                        continue

                    db_unknown_pkg = ReportUnknownPackage()
                    db_unknown_pkg.report = db_report
                    db_unknown_pkg.name = package["name"]
                    db_unknown_pkg.epoch = package["epoch"]
                    db_unknown_pkg.version = package["version"]
                    db_unknown_pkg.release = package["release"]
                    db_unknown_pkg.semver = to_semver(package["version"])
                    db_unknown_pkg.semrel = to_semver(package["release"])
                    db_unknown_pkg.arch = db_arch
                    db_unknown_pkg.type = role
                    db_unknown_pkg.count = 0
                    db.session.add(db_unknown_pkg)

                db_unknown_pkg.count += count
                continue

            db_reportpackage = get_reportpackage(db, db_report, db_package)
            if db_reportpackage is None:
                db_reportpackage = ReportPackage()
                db_reportpackage.report = db_report
                db_reportpackage.installed_package = db_package
                db_reportpackage.count = 0
                db_reportpackage.type = role
                db.session.add(db_reportpackage)

            db_reportpackage.count += count

    def validate_ureport(self, ureport) -> bool:
        CentOS.ureport_checker.check(ureport)
        return True

    def validate_packages(self, packages) -> bool:
        CentOS.packages_checker.check(packages)
        affected = False
        for package in packages:
            if "package_role" in package:
                if package["package_role"] not in CentOS.pkg_roles:
                    raise FafError("Only the following package roles are allowed: "
                                   "{0}".format(", ".join(CentOS.pkg_roles)))
                if package["package_role"] == "affected":
                    affected = True

        if not(affected or self.allow_unpackaged):
            raise FafError("uReport must contain affected package")

        return True

    def save_ureport(self, db, db_report, ureport, packages, flush=False, count=1) -> None:
        self._save_packages(db, db_report, packages, count=count)

        if flush:
            db.session.flush()

    def get_releases(self) -> Dict[str, Dict[str, str]]:
        result = {}

        for release in re.findall(r"[\w\.]+", self.inactive_releases):
            result[release] = {"status": "EOL"}
        for release in re.findall(r"[\w\.]+", self.active_releases):
            result[release] = {"status": "ACTIVE"}

        return result

    def get_components(self, release) -> List[str]:
        if not self.repo_urls:
            self.log_info("No repository URLs were found.")
            return []

        urls = [repo.replace("$releasever", release) for repo in self.repo_urls]
        components = []
        if "dnf" in repo_types:
            from pyfaf.repos.dnf import Dnf
            dnf = Dnf(self.name, *urls)
            components.extend(list(set(pkg["name"]
                                       for pkg in dnf.list_packages(["src"]))))
        else:
            raise FafError("No repo type available")
        return components

    def get_build_candidates(self, db) -> List[Build]:
        return (db.session.query(Build)
                .filter(Build.release.like("%%.el%%"))
                .all())

    def check_pkgname_match(self, packages, parser) -> bool:
        for package in packages:
            if ("package_role" not in package or
                    package["package_role"].lower() != "affected"):
                continue

            nvra = "{0}-{1}-{2}.{3}".format(package["name"],
                                            package["version"],
                                            package["release"],
                                            package["architecture"])

            match = parser.match(nvra)
            if match is not None:
                return True

        return False
