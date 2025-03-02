import datetime
import re
from collections import defaultdict
from operator import itemgetter
from hashlib import sha1

from typing import Any, List, Tuple

from flask import g

from sqlalchemy import asc

from wtforms import (Form,
                     SubmitField,
                     validators,
                     SelectMultipleField,
                     StringField,
                     SelectField,
                     FileField,
                     BooleanField)

from wtforms.ext.sqlalchemy.fields import (QuerySelectMultipleField,
                                           QuerySelectField)

from pyfaf.storage import OpSysRelease, OpSysComponent, Report, KernelTaintFlag
from pyfaf.storage.opsys import AssociatePeople, Arch
from pyfaf.problemtypes import problemtypes
from pyfaf.bugtrackers import bugtrackers
from pyfaf.queries import get_associate_by_name


class DaterangeField(StringField):
    date_format = "%Y-%m-%d"
    separator = ":"

    def __init__(self, label=None, validators_=None,
                 default_days=14,
                 **kwargs) -> None:
        self.default_days = default_days
        if default_days:
            today = datetime.date.today()
            kwargs["default"] = lambda: (
                today - datetime.timedelta(days=self.default_days), today)
        super().__init__(label, validators_, **kwargs)

    def process_formdata(self, valuelist) -> None:
        if valuelist:
            s = valuelist[0].split(self.separator)
            if len(s) == 2:
                self.data = (
                    datetime.datetime.strptime(s[0], self.date_format).date(),
                    datetime.datetime.strptime(s[1], self.date_format).date())

                return

        if self.default_days:
            today = datetime.date.today()
            self.data = (
                today - datetime.timedelta(days=self. default_days), today)
        else:
            self.data = None

    def _value(self) -> str:
        if self.data:
            return self.separator.join([d.strftime(self.date_format)
                                        for d in self.data[:2]])

        return ""


class TagListField(StringField):
    def _value(self) -> str:
        if self.data:
            return ", ".join(self.data)

        return ""

    def process_formdata(self, valuelist) -> None:
        if valuelist:
            self.data = [x.strip() for x in valuelist[0].split(",") if x.strip()]
        else:
            self.data = []


def component_list() -> List[Tuple[str, Any]]:
    sub = (db.session.query(Report.component_id)
           .filter(Report.component_id == OpSysComponent.id))
    comps = (db.session.query(OpSysComponent.id, OpSysComponent.name)
             .filter(sub.exists())
             .all())
    merged = defaultdict(list)
    for iden, name in comps:
        merged[name].append(iden)
    return sorted([(",".join(map(str, v)), k) for (k, v) in merged.items()],
                  key=itemgetter(1))


def component_names_to_ids(component_names) -> List[int]:
    """
    `component_names` must be a string with comma-separated component names.
    Returns [-1] if only invalid component_names were looked for.
    """
    component_ids = []
    if component_names:
        component_names = [x.strip() for x in component_names.split(",")]
        if component_names and component_names[0]:
            component_ids = list(map(itemgetter(0),
                                     (db.session.query(OpSysComponent.id)
                                      .filter(OpSysComponent.name.in_(component_names))
                                      .all())))

        # Some components were searched for but non was found in DB
        if not component_ids:
            component_ids = [-1]  # Put there some non-existing value

    return component_ids


releases_multiselect = QuerySelectMultipleField(
    "Releases",
    query_factory=lambda: (db.session.query(OpSysRelease)
                           .filter(OpSysRelease.status != "EOL")
                           .order_by(OpSysRelease.opsys_id)
                           .order_by(OpSysRelease.version)
                           .all()),
    get_pk=lambda a: a.id, get_label=lambda a: str(a)) # pylint: disable=unnecessary-lambda


arch_multiselect = QuerySelectMultipleField(
    "Architecture",
    query_factory=lambda: (db.session.query(Arch)
                           .order_by(Arch.name)
                           .all()),
    get_pk=lambda a: a.id, get_label=lambda a: str(a)) # pylint: disable=unnecessary-lambda


def maintainer_default():
    if g.user is not None:
        associate = get_associate_by_name(db, g.user.username)
        if associate is not None:
            return associate
    return None

associate_select = QuerySelectField(
    "Associate or Group",
    allow_blank=True,
    blank_text="Associate or Group",
    query_factory=lambda: (db.session.query(AssociatePeople)
                           .order_by(asc(AssociatePeople.name))
                           .all()),
    get_pk=lambda a: a.id, get_label=lambda a: a.name,
    default=maintainer_default)


type_multiselect = SelectMultipleField(
    "Type",
    choices=[(a, a) for a in sorted(problemtypes.keys())])

solution_checkbox = BooleanField("Solution")


class ProblemFilterForm(Form):
    opsysreleases = releases_multiselect

    component_names = StringField()

    daterange = DaterangeField(
        "Date range",
        default_days=14)

    associate = associate_select

    arch = arch_multiselect

    type = type_multiselect

    exclude_taintflags = QuerySelectMultipleField(
        "Exclude taintflags",
        query_factory=lambda: (db.session.query(KernelTaintFlag)
                               .order_by(KernelTaintFlag.character)
                               .all()),
        get_pk=lambda a: a.id, get_label=lambda a: "{0} {1}".format(a.character, a.ureport_name))

    function_names = TagListField()
    binary_names = TagListField()
    source_file_names = TagListField()

    since_version = StringField()
    since_release = StringField()

    to_version = StringField()
    to_release = StringField()

    solution = solution_checkbox

    probable_fix_osrs = QuerySelectMultipleField(
        "Probably fixed in",
        query_factory=lambda: (db.session.query(OpSysRelease)
                               .filter(OpSysRelease.status != "EOL")
                               .order_by(OpSysRelease.releasedate)
                               .all()),
        get_pk=lambda a: a.id, get_label=lambda a: str(a)) # pylint: disable=unnecessary-lambda

    bug_filter = SelectField("Bug status", validators=[validators.Optional()],
                             choices=[
                                 ("None", "Any bug status"),
                                 ("HAS_BUG", "Has a bug"),
                                 ("NO_BUGS", "No bugs"),
                                 ("HAS_OPEN_BUG", "Has an open bug"),
                                 ("ALL_BUGS_CLOSED", "All bugs closed")
                             ])

    def caching_key(self) -> str:
        associate = ()
        if self.associate.data:
            associate = (self.associate.data)

        return sha1(("ProblemFilterForm" + str((
            associate,
            tuple(self.arch.data or []),
            tuple(self.type.data or []),
            tuple(self.exclude_taintflags.data or []),
            tuple(sorted(self.component_names.data or [])),
            tuple(self.daterange.data or []),
            tuple(sorted(self.opsysreleases.data or [])),
            tuple(sorted(self.function_names.data or [])),
            tuple(sorted(self.binary_names.data or [])),
            tuple(sorted(self.source_file_names.data or [])),
            tuple(sorted(self.since_version.data or [])),
            tuple(sorted(self.since_release.data or [])),
            tuple(sorted(self.to_version.data or [])),
            tuple(sorted(self.to_release.data or [])),
            tuple(sorted(self.probable_fix_osrs.data or [])),
            tuple(sorted(self.bug_filter.data or [])),
            ))).encode("utf-8")).hexdigest()


class ReportFilterForm(Form):
    opsysreleases = releases_multiselect

    component_names = StringField()

    daterange = DaterangeField(
        "Date range",
        validators_=[validators.Optional()],
        default_days=None)

    associate = associate_select

    arch = arch_multiselect

    type = type_multiselect

    solution = solution_checkbox

    order_by = SelectField("Order by", choices=[
        ("last_occurrence", "Last occurrence"),
        ("first_occurrence", "First occurrence"),
        ("count", "Count")],
                           default="last_occurrence")

    def caching_key(self) -> str:
        associate = ()
        if self.associate.data:
            associate = (self.associate.data)

        return sha1(("ReportFilterForm" + str((
            associate,
            tuple(self.arch.data or []),
            tuple(self.type.data or []),
            tuple(sorted(self.component_names.data or [])),
            tuple(self.daterange.data or []),
            tuple(self.order_by.data or []),
            tuple(sorted(self.opsysreleases.data or []))))).encode("utf-8")).hexdigest()


class SummaryForm(Form):
    opsysreleases = releases_multiselect

    component_names = StringField()

    daterange = DaterangeField(
        "Date range",
        default_days=14)

    resolution = SelectField("Time unit", choices=[
        ("d", "daily"),
        ("w", "weekly"),
        ("m", "monthly")],
                             default="d")

    def caching_key(self) -> str:
        return sha1(("SummaryForm" + str((
            tuple(self.resolution.data or []),
            tuple(sorted(self.component_names.data or [])),
            tuple(self.daterange.data or []),
            tuple(sorted(self.opsysreleases.data or []))))).encode("utf-8")).hexdigest()


class BacktraceDiffForm(Form):
    lhs = SelectField("LHS")
    rhs = SelectField("RHS")


class NewReportForm(Form):
    file = FileField("uReport file")


class NewAttachmentForm(Form):
    file = FileField("Attachment file")


class BugIdField(StringField):
    def _value(self) -> str:
        if self.data:
            return str(self.data)

        return ""

    def process_formdata(self, valuelist) -> None:
        if valuelist:
            for value in valuelist:
                try:
                    self.data = int(value)
                except ValueError as ex:
                    m = re.search(r"id=(\d+)", value)
                    if m is None:
                        raise validators.ValidationError("Invalid Bug ID") from ex
                    self.data = int(m.group(1))
        else:
            self.data = None


class AssociateBzForm(Form):
    bug_id = BugIdField("Bug ID or URL")
    bugtracker = SelectField("Bugtracker", choices=[
        (name, name) for name in bugtrackers])


class DissociateBzForm(Form):
    bug_id = BugIdField("Bug ID")


class ProblemComponents(Form):
    component_names = StringField()
    submit = SubmitField("Reassign")


# has to be at the end to avoid circular imports
from webfaf.webfaf_main import db # pylint: disable=wrong-import-position
