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

from __future__ import unicode_literals

from typing import List, Optional

import os
import shutil
import satyr
from pyfaf.problemtypes import ProblemType
from pyfaf.checker import (Checker,
                           DictChecker,
                           IntChecker,
                           ListChecker,
                           StringChecker)
from pyfaf.common import FafError, get_libname
from pyfaf.queries import (get_package_by_file,
                           get_package_by_file_build_arch,
                           get_reportexe,
                           get_src_package_by_build,
                           get_ssource_by_bpo,
                           get_symbol_by_name_path)
from pyfaf.retrace import (addr2line,
                           demangle,
                           get_base_address,
                           ssource2funcname,
                           usrmove)
from pyfaf.storage import (OpSysComponent,
                           Report,
                           ReportBacktrace,
                           ReportBtFrame,
                           ReportBtHash,
                           ReportBtThread,
                           ReportExecutable,
                           Symbol,
                           SymbolSource,
                           column_len)
from pyfaf.utils.parse import str2bool
from pyfaf.utils.hash import hash_list

__all__ = ["CoredumpProblem"]


class CoredumpProblem(ProblemType):
    name = "core"
    nice_name = "Crash of user-space binary"

    checker = DictChecker({
        # no need to check type twice, the toplevel checker already did it
        # "type": StringChecker(allowed=[CoredumpProblem.name]),
        "signal":     IntChecker(minval=0),
        "component":  StringChecker(pattern=r"^[a-zA-Z0-9\-\._]+$",
                                    maxlen=column_len(OpSysComponent, "name")),
        "executable": StringChecker(maxlen=column_len(ReportExecutable,
                                                      "path")),
        "user":       DictChecker({
            "root":       Checker(bool),
            "local":      Checker(bool),
        }),
        "stacktrace": ListChecker(DictChecker({
            "crash_thread": Checker(bool, mandatory=False),
            "frames":       ListChecker(DictChecker({
                "address":         IntChecker(minval=0),
                "build_id_offset": IntChecker(minval=0),
                "file_name":       StringChecker(maxlen=column_len(SymbolSource,
                                                                   "path")),
                "build_id": StringChecker(pattern=r"^[a-fA-F0-9]+$",
                                          maxlen=column_len(SymbolSource,
                                                            "build_id"),
                                          mandatory=False),
                "fingerprint": StringChecker(pattern=r"^[a-fA-F0-9]+$",
                                             maxlen=column_len(ReportBtHash,
                                                               "hash"),
                                             mandatory=False),
                "function_name": StringChecker(maxlen=column_len(Symbol,
                                                                 "nice_name"), mandatory=False)

            }), minlen=1)
        }), minlen=1)
    })

    def __init__(self, *args, **kwargs) -> None:
        super().__init__()

        hashkeys = ["processing.corehashframes", "processing.hashframes"]
        self.hashframes = None
        self.load_config_to_self("hashframes", hashkeys, 16, callback=int)

        cmpkeys = ["processing.corecmpframes", "processing.cmpframes",
                   "processing.clusterframes"]
        self.cmpframes = None
        self.load_config_to_self("cmpframes", cmpkeys, 16, callback=int)

        cutkeys = ["processing.corecutthreshold", "processing.cutthreshold"]
        self.cutthreshold = None
        self.load_config_to_self("cutthreshold", cutkeys, 0.3, callback=float)

        normkeys = ["processing.corenormalize", "processing.normalize"]
        self.normalize = None
        self.load_config_to_self("normalize", normkeys, True, callback=str2bool)

        skipkeys = ["retrace.coreskipsource", "retrace.skipsource"]
        self.skipsrc = None
        self.load_config_to_self("skipsrc", skipkeys, True, callback=str2bool)

    def _get_crash_thread(self, stacktrace):
        """
        Searches for a single crash thread and return it. Raises FafError if
        there is no crash thread or if there are multiple crash threads.
        """

        crashthreads = [t for t in stacktrace if ("crash_thread" in t and
                                                  t["crash_thread"])]
        if not crashthreads:
            raise FafError("No crash thread found")

        if len(crashthreads) > 1:
            raise FafError("Multiple crash threads found")

        return crashthreads[0]["frames"]

    def _hash_backtrace(self, backtrace):
        result = []

        for key in ["function_name", "fingerprint", "build_id_offset"]:
            hashbase = []

            threads_sane = []
            for thread in backtrace:
                threads_sane.append(all(key in f for f in thread["frames"]))

            if not all(threads_sane):
                continue

            for thread in backtrace:
                if "crash_thread" in thread and thread["crash_thread"]:
                    hashbase.append("Crash Thread")
                else:
                    hashbase.append("Thread")

                for frame in thread["frames"]:
                    if "build_id" in frame:
                        build_id = frame["build_id"]
                    else:
                        build_id = None

                    hashbase.append("  {0} @ {1} ({2})"
                                    .format(frame[key],
                                            frame["file_name"].encode("ascii",
                                                                      "ignore"),
                                            build_id))

            result.append(hash_list(hashbase))

        return result

    def _db_thread_to_satyr(self, db_thread) -> satyr.GdbThread:
        self.log_debug("Creating threads using satyr")

        thread = satyr.GdbThread()
        thread.number = db_thread.number

        for db_frame in db_thread.frames:
            frame = satyr.GdbFrame()
            frame.address = db_frame.symbolsource.offset
            frame.library_name = db_frame.symbolsource.path
            frame.number = db_frame.order
            if db_frame.symbolsource.symbol is not None:
                frame.function_name = db_frame.symbolsource.symbol.name
            else:
                frame.function_name = "??"

            if db_frame.symbolsource.source_path is not None:
                frame.source_file = db_frame.symbolsource.source_path

            if db_frame.symbolsource.line_number is not None:
                frame.source_line = db_frame.symbolsource.line_number

            thread.frames.append(frame)

        if self.normalize:
            stacktrace = satyr.GdbStacktrace()
            stacktrace.threads.append(thread)
            stacktrace.normalize()

        return thread

    def _db_thread_validate(self, db_thread) -> bool:
        if len(db_thread.frames) == 1:
            db_frame = db_thread.frames[0]
            if (db_frame.symbolsource.symbol is not None and
                    db_frame.symbolsource.symbol.name ==
                    "anonymous function" and
                    db_frame.symbolsource.symbol.normalized_path ==
                    "unknown filename"):

                return False
        return True

    def db_report_to_satyr(self, db_report):
        if not db_report.backtraces:
            self.log_warn("Report #{0} has no usable backtraces"
                          .format(db_report.id))
            return None

        if not db_report.backtraces[0].threads:
            self.log_warn("Report #{0} has no usable threads"
                          .format(db_report.id))
            return None

        for db_thread in db_report.backtraces[0].threads:
            if not db_thread.crashthread:
                continue
            if self._db_thread_validate(db_thread):
                return self._db_thread_to_satyr(db_thread)

            self.log_warn("Report #{0} has only one bad frame"
                          .format(db_report.id))
            return None

        self.log_warn("Report #{0} has no crash thread".format(db_report.id))
        return None

    def _build_id_to_debug_files(self, build_id) -> List[str]:
        return ["/usr/lib/debug/.build-id/{0}/{1}.debug".format(build_id[:2],
                                                                build_id[2:]),
                "/usr/lib/.build-id/{0}/{1}".format(build_id[:2], build_id[2:])]

    def validate_ureport(self, ureport) -> bool:
        # Frames calling JIT compiled functions usually do not contain
        # function name nor file name. This would result to the uReport being
        # rejected. However the stack above is often the relevant part and we
        # do not want to reject such uReports.
        # This code tries to detect calling JIT compiled code and filling
        # the frames with file name (the JIT caller) and function name
        # (anonymous function).
        if "stacktrace" in ureport and isinstance(ureport["stacktrace"], list):
            for thread in ureport["stacktrace"]:
                if not isinstance(thread, dict):
                    continue

                jit_fname = None
                if "frames" in thread and isinstance(thread["frames"], list):
                    for frame in thread["frames"]:
                        if not isinstance(frame, dict):
                            continue

                        if ("file_name" in frame and
                                "function_name" in frame and
                                "jit" in frame["function_name"].lower()):

                            jit_fname = frame["file_name"]

                        if "file_name" not in frame and jit_fname is not None:
                            frame["file_name"] = jit_fname
                            if ("function_name" not in frame or
                                    frame["function_name"] == "??"):

                                frame["function_name"] = "anonymous function"

                    if thread["frames"]:
                        last_frame = thread["frames"][-1]
                        if isinstance(last_frame, dict):
                            if "file_name" not in last_frame:
                                last_frame["file_name"] = "unknown filename"
                            if ("function_name" not in last_frame or
                                    last_frame["function_name"] == "??"):

                                last_frame["function_name"] = "anonymous function"

        CoredumpProblem.checker.check(ureport)

        # just to be sure there is exactly one crash thread
        self._get_crash_thread(ureport["stacktrace"])
        return True

    def hash_ureport(self, ureport):
        crashthread = self._get_crash_thread(ureport["stacktrace"])
        hashbase = [ureport["component"]]

        if all("function_name" in f for f in crashthread):
            key = "function_name"
        elif all("fingerprint" in f for f in crashthread):
            key = "fingerprint"
        else:
            key = "build_id_offset"

        for i, frame in enumerate(crashthread):
            # Instance of 'CoredumpProblem' has no 'hashframes' member
            # pylint: disable-msg=E1101
            if i >= self.hashframes:
                break

            hashbase.append("{0} @ {1}".format(frame[key], frame["file_name"]))

        return hash_list(hashbase)

    def save_ureport(self, db, db_report, ureport, flush=False, count=1) -> None:
        db_report.errname = str(ureport["signal"])

        db_reportexe = get_reportexe(db, db_report, ureport["executable"])
        if db_reportexe is None:
            db_reportexe = ReportExecutable()
            db_reportexe.path = ureport["executable"]
            db_reportexe.report = db_report
            db_reportexe.count = 0
            db.session.add(db_reportexe)

        db_reportexe.count += count

        bthashes = self._hash_backtrace(ureport["stacktrace"])
        if not bthashes:
            raise FafError("Unable to get backtrace hash")

        if not db_report.backtraces:
            new_symbols = {}
            new_symbolsources = {}

            db_backtrace = ReportBacktrace()
            db_backtrace.report = db_report
            db.session.add(db_backtrace)

            for bthash in bthashes:
                db_bthash = ReportBtHash()
                db_bthash.backtrace = db_backtrace
                db_bthash.type = "NAMES"
                db_bthash.hash = bthash
                db.session.add(db_bthash)

            tid = 0
            for thread in ureport["stacktrace"]:
                tid += 1

                crash = "crash_thread" in thread and thread["crash_thread"]
                db_thread = ReportBtThread()
                db_thread.backtrace = db_backtrace
                db_thread.number = tid
                db_thread.crashthread = crash
                db.session.add(db_thread)

                fid = 0
                for frame in thread["frames"]:
                    # OK, this is totally ugly.
                    # Frames may contain inlined functions, that would normally
                    # require shifting all frames by 1 and inserting a new one.
                    # There is no way to do this efficiently with SQL Alchemy
                    # (you need to go one by one and flush after each) so
                    # creating a space for additional frames is a huge speed
                    # optimization.
                    fid += 10

                    if "build_id" in frame:
                        build_id = frame["build_id"]
                    else:
                        build_id = None

                    if "fingerprint" in frame:
                        fingerprint = frame["fingerprint"]
                    else:
                        fingerprint = None

                    path = os.path.abspath(frame["file_name"])
                    offset = frame["build_id_offset"]

                    db_symbol = None
                    if "function_name" in frame:
                        norm_path = get_libname(path)

                        db_symbol = \
                            get_symbol_by_name_path(db,
                                                    frame["function_name"],
                                                    norm_path)
                        if db_symbol is None:
                            key = (frame["function_name"], norm_path)
                            if key in new_symbols:
                                db_symbol = new_symbols[key]
                            else:
                                db_symbol = Symbol()
                                db_symbol.name = frame["function_name"]
                                db_symbol.normalized_path = norm_path
                                db.session.add(db_symbol)
                                new_symbols[key] = db_symbol

                    db_symbolsource = get_ssource_by_bpo(db, build_id,
                                                         path, offset)

                    if db_symbolsource is None:
                        key = (build_id, path, offset)

                        if key in new_symbolsources:
                            db_symbolsource = new_symbolsources[key]
                        else:
                            db_symbolsource = SymbolSource()
                            db_symbolsource.symbol = db_symbol
                            db_symbolsource.build_id = build_id
                            db_symbolsource.path = path
                            db_symbolsource.offset = offset
                            db_symbolsource.hash = fingerprint
                            db.session.add(db_symbolsource)
                            new_symbolsources[key] = db_symbolsource

                    db_frame = ReportBtFrame()
                    db_frame.thread = db_thread
                    db_frame.order = fid
                    db_frame.symbolsource = db_symbolsource
                    db_frame.inlined = False
                    db.session.add(db_frame)

        if flush:
            db.session.flush()

    def save_ureport_post_flush(self) -> None:
        self.log_debug("save_ureport_post_flush is not required for coredumps")

    def get_component_name(self, ureport) -> str:
        return ureport["component"]

    def compare(self, db_report1, db_report2):
        satyr_report1 = self.db_report_to_satyr(db_report1)
        satyr_report2 = self.db_report_to_satyr(db_report2)
        return satyr_report1.distance(satyr_report2)

    def check_btpath_match(self, ureport, parser) -> bool:
        crash_thread = None
        for thread in ureport["stacktrace"]:
            if "crash_thread" not in thread or not thread["crash_thread"]:
                continue
            crash_thread = thread

        for frame in crash_thread["frames"]:
            match = parser.match(frame["file_name"])

            if match is not None:
                return True

        return False

    def _get_ssources_for_retrace_query(self, db):
        core_syms = (db.session.query(SymbolSource.id)
                     .join(ReportBtFrame)
                     .join(ReportBtThread)
                     .join(ReportBacktrace)
                     .join(Report)
                     .filter(Report.type == CoredumpProblem.name)
                     .subquery())

        q = (db.session.query(SymbolSource)
             .filter(SymbolSource.id.in_(core_syms))
             .filter(SymbolSource.build_id.isnot(None))
             .filter((SymbolSource.symbol_id.is_(None)) |
                     (SymbolSource.source_path.is_(None)) |
                     (SymbolSource.line_number.is_(None))))
        return q

    def find_packages_for_ssource(self, db, db_ssource):
        self.log_debug("Build-id: %s", db_ssource.build_id)
        files = self._build_id_to_debug_files(db_ssource.build_id)
        self.log_debug("File names: %s", ", ".join(files))
        db_debug_package = get_package_by_file(db, files)
        if db_debug_package is None:
            debug_nvra = "Not found"
        else:
            debug_nvra = db_debug_package.nvra()

        self.log_debug("Debug Package: %s", debug_nvra)

        db_bin_package = None

        if db_debug_package is not None:
            paths = [db_ssource.path]
            if os.path.sep in db_ssource.path:
                paths.append(usrmove(db_ssource.path))
                paths.append(os.path.abspath(db_ssource.path))
                paths.append(usrmove(os.path.abspath(db_ssource.path)))

            db_build = db_debug_package.build
            db_arch = db_debug_package.arch
            for path in paths:
                db_bin_package = get_package_by_file_build_arch(db, path,
                                                                db_build,
                                                                db_arch)

                if db_bin_package is not None:
                    break

        if db_bin_package is None:
            bin_nvra = "Not found"
        else:
            bin_nvra = db_bin_package.nvra()

        self.log_debug("Binary Package: %s", bin_nvra)

        db_src_package = None

        if not self.skipsrc and db_debug_package is not None:
            db_build = db_debug_package.build
            db_src_package = get_src_package_by_build(db, db_build)

        if db_src_package is None:
            src_nvra = "Not found"
        else:
            src_nvra = db_src_package.nvra()

        self.log_debug("Source Package: %s", src_nvra)

        # indicate incomplete result
        if db_bin_package is None:
            db_debug_package = None

        return db_ssource, (db_debug_package, db_bin_package, db_src_package)

    def retrace(self, db, task) -> None:
        new_symbols = {}
        new_symbolsources = {}

        for bin_pkg, db_ssources in task.binary_packages.items():
            self.log_info("Retracing symbols from package {0}"
                          .format(bin_pkg.nvra))

            i = 0
            for db_ssource in db_ssources:
                i += 1

                self.log_debug("[%d / %d] Processing '%s' @ '%s'",
                               i, len(db_ssources), ssource2funcname(db_ssource),
                               db_ssource.path)

                norm_path = get_libname(db_ssource.path)

                if bin_pkg.unpacked_path is None:
                    self.log_debug("fail: path to unpacked binary package not found")
                    db_ssource.retrace_fail_count += 1
                    continue

                binary = os.path.join(bin_pkg.unpacked_path, db_ssource.path[1:])

                try:
                    address = get_base_address(binary) + db_ssource.offset
                except FafError as ex:
                    self.log_debug("get_base_address failed: %s", str(ex))
                    db_ssource.retrace_fail_count += 1
                    continue

                try:
                    debug_path = os.path.join(task.debuginfo.unpacked_path,
                                              "usr", "lib", "debug")
                    results = addr2line(binary, address, debug_path)
                    results.reverse()
                except Exception as ex: # pylint: disable=broad-except
                    self.log_debug("addr2line failed: %s", str(ex))
                    db_ssource.retrace_fail_count += 1
                    continue

                inl_id = 0
                while len(results) > 1:
                    inl_id += 1

                    funcname, srcfile, srcline = results.pop()
                    self.log_debug("Unwinding inlined function '%s'", funcname)
                    # hack - we have no offset for inlined symbols
                    # let's use minus source line to avoid collisions
                    offset = -srcline

                    db_ssource_inl = get_ssource_by_bpo(db, db_ssource.build_id,
                                                        db_ssource.path, offset)
                    if db_ssource_inl is None:
                        key = (db_ssource.build_id, db_ssource.path, offset)
                        if key in new_symbolsources:
                            db_ssource_inl = new_symbolsources[key]
                        else:
                            db_symbol_inl = get_symbol_by_name_path(db,
                                                                    funcname,
                                                                    norm_path)
                            if db_symbol_inl is None:
                                sym_key = (funcname, norm_path)
                                if sym_key in new_symbols:
                                    db_symbol_inl = new_symbols[sym_key]
                                else:
                                    db_symbol_inl = Symbol()
                                    db_symbol_inl.name = funcname
                                    db_symbol_inl.normalized_path = norm_path
                                    db.session.add(db_symbol_inl)
                                    new_symbols[sym_key] = db_symbol_inl

                            db_ssource_inl = SymbolSource()
                            db_ssource_inl.symbol = db_symbol_inl
                            db_ssource_inl.build_id = db_ssource.build_id
                            db_ssource_inl.path = db_ssource.path
                            db_ssource_inl.offset = offset
                            db_ssource_inl.source_path = srcfile
                            db_ssource_inl.line_number = srcline
                            db.session.add(db_ssource_inl)
                            new_symbolsources[key] = db_ssource_inl

                    for db_frame in db_ssource.frames:
                        db_frames = sorted(db_frame.thread.frames,
                                           key=lambda f: f.order)
                        idx = db_frames.index(db_frame)
                        if idx > 0:
                            prevframe = db_frame.thread.frames[idx - 1]
                            if (prevframe.inlined and
                                    prevframe.symbolsource == db_ssource_inl):

                                continue

                        db_newframe = ReportBtFrame()
                        db_newframe.symbolsource = db_ssource_inl
                        db_newframe.thread = db_frame.thread
                        db_newframe.inlined = True
                        db_newframe.order = db_frame.order - inl_id
                        db.session.add(db_newframe)

                funcname, srcfile, srcline = results.pop()
                self.log_debug("Result: %s", funcname)
                db_symbol = get_symbol_by_name_path(db, funcname, norm_path)
                if db_symbol is None:
                    key = (funcname, norm_path)
                    if key in new_symbols:
                        db_symbol = new_symbols[key]
                    else:
                        self.log_debug("Creating new symbol '%s' @ '%s'", funcname, db_ssource.path)
                        db_symbol = Symbol()
                        db_symbol.name = funcname
                        db_symbol.normalized_path = norm_path
                        db.session.add(db_symbol)

                        new_symbols[key] = db_symbol

                if db_symbol.nice_name is None:
                    db_symbol.nice_name = demangle(funcname)

                db_ssource.symbol = db_symbol
                db_ssource.source_path = srcfile
                db_ssource.line_number = srcline

        if task.debuginfo.unpacked_path is not None:
            self.log_debug("Removing %s", task.debuginfo.unpacked_path)
            shutil.rmtree(task.debuginfo.unpacked_path, ignore_errors=True)

        if task.source is not None and task.source.unpacked_path is not None:
            self.log_debug("Removing %s", task.source.unpacked_path)
            shutil.rmtree(task.source.unpacked_path, ignore_errors=True)

        for bin_pkg in task.binary_packages.keys():
            if bin_pkg.unpacked_path is not None:
                self.log_debug("Removing %s", bin_pkg.unpacked_path)
                shutil.rmtree(bin_pkg.unpacked_path, ignore_errors=True)

    def find_crash_function(self, db_backtrace) -> Optional[str]:
        for db_thread in db_backtrace.threads:
            if not db_thread.crashthread:
                continue

            satyr_thread = self._db_thread_to_satyr(db_thread)
            satyr_stacktrace = satyr.GdbStacktrace()
            satyr_stacktrace.threads.append(satyr_thread)

            return satyr_stacktrace.find_crash_frame().function_name

        self.log_warn("Backtrace #{0} has no crash thread"
                      .format(db_backtrace.id))
        return None
