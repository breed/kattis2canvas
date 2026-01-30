import collections
import concurrent.futures
import configparser
import datetime
import os
import re
import sys
from fractions import Fraction
from typing import NamedTuple, Optional

import click
from dateutil import parser as dateparser
import pprint
import requests
import requests.cookies
import requests.exceptions
from bs4 import BeautifulSoup
from canvasapi import Canvas, module
from canvasapi.course import Course
from canvasapi.user import User

HEADERS = {'User-Agent': 'kattis-to-canvas'}


class Config(NamedTuple):
    kattis_username: str
    kattis_token: str
    kattis_loginurl: str
    kattis_hostname: str
    canvas_url: str
    canvas_token: str
    kattis_password: str = ""  # Optional password for web access


config: Optional[Config] = None
kattis_session: Optional[requests.Session] = None


class Student(NamedTuple):
    kattis_url: str
    name: str
    email: str
    canvas_id: str


class Submission(NamedTuple):
    user: str
    problem: str
    score: float
    url: str
    date: datetime.datetime


now = datetime.datetime.now(datetime.timezone.utc)


def error(message: str):
    click.echo(click.style(message, fg='red'))


def info(message: str):
    click.echo(click.style(message, fg='blue'))


def warn(message: str):
    click.echo(click.style(message, fg='yellow'))


def check_status(rsp: requests.Response):
    if rsp.status_code != 200:
        error(f"got status {rsp.status_code} for {rsp.url}.")
        exit(6)


# return the last element of a URL
def extract_last(pathish: str) -> str:
    last_slash = pathish.rindex("/")
    if last_slash:
        pathish = pathish[last_slash + 1:]
    return pathish


# for debugging
def introspect(o):
    print("class", o.__class__)
    for i in dir(o):
        print(i)


def web_get(url: str) -> requests.Response:
    rsp: requests.Response = kattis_session.get(url)
    check_status(rsp)
    return rsp


def get_config_path():
    return click.get_app_dir("kattis2canvas.ini")


def load_config():
    """Load config and login to Kattis. Called by commands that need it."""
    global config, kattis_session
    if config is not None:
        return  # Already loaded

    config_ini = get_config_path()
    parser = configparser.ConfigParser()
    parser.read([config_ini])
    try:
        config = Config(
            kattis_username=parser['kattis']['username'],
            kattis_token=parser['kattis']['token'],
            kattis_hostname=parser['kattis']['hostname'],
            kattis_loginurl=parser['kattis']['loginurl'],
            canvas_url=parser['canvas']['url'],
            canvas_token=parser['canvas']['token'],
            kattis_password=parser['kattis'].get('password', ''),
        )
    except:
        print(f"""problem getting configuration from {config_ini}. should have the following lines:

[kattis]
username=kattis_username
token=kattis_token
password=kattis_password  # optional, for web access to submissions
hostname: something_like_sjsu.kattis.com
loginurl: https://something_like_sjsu.kattis.com/login
[canvas]
url=https://something_like_sjsu.instructure.com
token=canvas_token

Run 'kattis2canvas setup' to configure.
""")
        exit(2)

    # Create session and login to Kattis
    kattis_session = requests.Session()
    kattis_session.headers.update(HEADERS)

    # Get CSRF token from login page
    rsp = kattis_session.get(config.kattis_loginurl)
    bs = BeautifulSoup(rsp.content, 'html.parser')
    csrf_input = bs.find('input', {'name': 'csrf_token'})
    csrf_token = csrf_input['value'] if csrf_input else None

    # Login with password if available, otherwise try token
    if config.kattis_password:
        args = {'user': config.kattis_username, 'password': config.kattis_password, 'csrf_token': csrf_token}
    else:
        args = {'user': config.kattis_username, 'token': config.kattis_token, 'csrf_token': csrf_token}
    rsp = kattis_session.post(config.kattis_loginurl, data=args)

    # Verify login by checking if we can access a protected page
    rsp = kattis_session.get(f"https://{config.kattis_hostname}/")
    bs = BeautifulSoup(rsp.content, 'html.parser')
    login_link = bs.find('a', string='Log in')
    if login_link:
        error("Kattis web login failed.")
        if not config.kattis_password:
            error("The API token only works for submissions, not web access.")
            error("Add 'password=your_kattis_password' to the [kattis] section in your config file.")
        else:
            error("Check your username and password.")
        exit(2)


@click.group()
def top():
    pass


def test_kattis_login(username, password, loginurl, hostname):
    """Test if Kattis credentials work for web access. Returns True if successful."""
    if not all([username, password, loginurl, hostname]):
        return False
    try:
        session = requests.Session()
        session.headers.update(HEADERS)

        # Get CSRF token
        rsp = session.get(loginurl)
        bs = BeautifulSoup(rsp.content, 'html.parser')
        csrf_input = bs.find('input', {'name': 'csrf_token'})
        csrf_token = csrf_input['value'] if csrf_input else None

        # Login with password
        args = {'user': username, 'password': password, 'csrf_token': csrf_token}
        session.post(loginurl, data=args)

        # Check if actually logged in
        rsp = session.get(f"https://{hostname}/")
        bs = BeautifulSoup(rsp.content, 'html.parser')
        login_link = bs.find('a', string='Log in')
        return login_link is None
    except:
        return False


def test_canvas_login(url, token):
    """Test if Canvas credentials work. Returns True if successful."""
    if not all([url, token]):
        return False
    try:
        canvas = Canvas(url, token)
        # Try to get current user - this will fail if credentials are bad
        canvas.get_current_user()
        return True
    except:
        return False


@top.command()
def setup():
    """
    Set up or update Kattis and Canvas credentials.
    """
    config_ini = get_config_path()
    parser = configparser.ConfigParser()
    parser.read([config_ini])

    # Ensure sections exist
    if 'kattis' not in parser:
        parser['kattis'] = {}
    if 'canvas' not in parser:
        parser['canvas'] = {}

    config_changed = False

    # Test existing Kattis credentials
    kattis_username = parser['kattis'].get('username', '')
    kattis_password = parser['kattis'].get('password', '')
    kattis_hostname = parser['kattis'].get('hostname', '')
    kattis_loginurl = parser['kattis'].get('loginurl', '')

    info("=== Kattis Configuration ===")
    if test_kattis_login(kattis_username, kattis_password, kattis_loginurl, kattis_hostname):
        info(f"Kattis login OK (user: {kattis_username}, host: {kattis_hostname})")
    else:
        if kattis_username:
            warn(f"Kattis web login failed for {kattis_username}")

        if kattis_hostname:
            kattisrc_url = f"https://{kattis_hostname}/download/kattisrc"
        else:
            kattisrc_url = "https://<your-school>.kattis.com/download/kattisrc"

        info(f"Go to: {kattisrc_url}")
        info("(Log in if needed, then copy the entire contents of the file)")
        info("")
        info("Paste the contents of your .kattisrc file below, then press Enter twice:")

        lines = []
        while True:
            try:
                line = input()
                if line == "" and lines and lines[-1] == "":
                    break
                lines.append(line)
            except EOFError:
                break

        kattisrc_content = "\n".join(lines)

        if kattisrc_content.strip():
            kattisrc = configparser.ConfigParser()
            kattisrc.read_string(kattisrc_content)

            try:
                parser['kattis']['username'] = kattisrc['user']['username']
                parser['kattis']['token'] = kattisrc['user']['token']
                parser['kattis']['hostname'] = kattisrc['kattis']['hostname']
                # Ensure loginurl ends with /login
                loginurl = kattisrc['kattis']['loginurl']
                if not loginurl.endswith('/login'):
                    loginurl = loginurl.rstrip('/') + '/login'
                parser['kattis']['loginurl'] = loginurl
                config_changed = True
                info("Kattis config from .kattisrc saved.")
            except KeyError as e:
                error(f"Could not parse .kattisrc content: missing {e}")
                return
        else:
            info("No .kattisrc content provided.")

        # Ask for password for web access
        info("")
        info("The Kattis API token only works for submissions, not web access.")
        info("To access the submissions page, you need your Kattis password.")
        kattis_password = click.prompt("Kattis password (for web access)",
                                       default=parser['kattis'].get('password', ''),
                                       hide_input=True)
        if kattis_password:
            parser['kattis']['password'] = kattis_password
            config_changed = True

            # Verify the credentials work
            if test_kattis_login(parser['kattis'].get('username', ''), kattis_password,
                               parser['kattis'].get('loginurl', ''), parser['kattis'].get('hostname', '')):
                info("Kattis web login verified.")
            else:
                warn("Kattis web login failed. Check your password.")

    info("")
    info("=== Canvas Configuration ===")
    canvas_url = parser['canvas'].get('url', '')
    canvas_token = parser['canvas'].get('token', '')

    if test_canvas_login(canvas_url, canvas_token):
        info(f"Canvas login OK ({canvas_url})")
    else:
        if canvas_url:
            warn(f"Canvas login failed for {canvas_url}")

        canvas_url = click.prompt("Canvas URL (e.g., https://sjsu.instructure.com)",
                                  default=canvas_url)
        canvas_token = click.prompt("Canvas API token (from Account > Settings > New Access Token)",
                                    default=canvas_token)

        parser['canvas']['url'] = canvas_url
        parser['canvas']['token'] = canvas_token
        config_changed = True

        # Verify the new credentials work
        if test_canvas_login(canvas_url, canvas_token):
            info("Canvas credentials verified and saved.")
        else:
            error("Canvas login still failing with new credentials.")

    # Write config if changed
    if config_changed:
        os.makedirs(os.path.dirname(config_ini), exist_ok=True)
        with open(config_ini, 'w') as f:
            parser.write(f)
        info(f"Configuration saved to {config_ini}")
    else:
        info("All credentials OK, no changes needed.")


def get_offerings(offering_pattern: str) -> str:
    rsp = web_get(f"https://{config.kattis_hostname}/")
    bs = BeautifulSoup(rsp.content, 'html.parser')
    for a in bs.find_all('a'):
        h = a.get('href')
        if h and re.match("/courses/[^/]+/[^/]+", h) and offering_pattern in h:
            yield h


@top.command()
@click.argument("name", default="")
def list_offerings(name: str):
    """
    list the possible offerings.
    :param name: a substring of the offering name
    """
    load_config()
    for offering in get_offerings(name):
        info(str(offering))


# Common timezone abbreviations to UTC offsets
TZINFOS = {
    "UTC": 0,
    "GMT": 0,
    "CET": 3600,      # Central European Time (UTC+1)
    "CEST": 7200,     # Central European Summer Time (UTC+2)
    "EST": -18000,    # Eastern Standard Time (UTC-5)
    "EDT": -14400,    # Eastern Daylight Time (UTC-4)
    "CST": -21600,    # Central Standard Time (UTC-6)
    "CDT": -18000,    # Central Daylight Time (UTC-5)
    "MST": -25200,    # Mountain Standard Time (UTC-7)
    "MDT": -21600,    # Mountain Daylight Time (UTC-6)
    "PST": -28800,    # Pacific Standard Time (UTC-8)
    "PDT": -25200,    # Pacific Daylight Time (UTC-7)
}


# reformat kattis date format to canvas format (ISO 8601 UTC)
def extract_kattis_date(element: str) -> str:
    if element == "infinity":
        element = "2100-01-01 00:00 UTC"
    dt = dateparser.parse(element, tzinfos=TZINFOS)
    # Convert to UTC and format with Z suffix for Canvas API
    dt_utc = dt.astimezone(datetime.timezone.utc)
    return dt_utc.strftime("%Y-%m-%dT%H:%M:%SZ")


# convert canvas UTC to datetime
def extract_canvas_date(element: str) -> datetime.datetime:
    return datetime.datetime.strptime(element, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)


class Assignment(NamedTuple):
    url: str
    assignment_id: str
    title: str
    description: str
    start: str
    end: str


def get_assignments(offering: str) -> [Assignment]:
    rsp = web_get(f"https://{config.kattis_hostname}{offering}")
    bs = BeautifulSoup(rsp.content, 'html.parser')
    for a in bs.find_all('a'):
        h = a.get('href')
        if h and re.search(r"assignments/\w+$", h):
            url = f"https://{config.kattis_hostname}{h}"
            rsp2 = web_get(url)
            bs2 = BeautifulSoup(rsp2.content, 'html.parser')
            description_h2 = bs2.find("h2", string="Description", recursive=True)
            description = None
            if description_h2:
                p = description_h2.find_next_sibling("p")
                if p:
                    description = p.text
            all_td = iter(bs2.find_all("td"))
            start = None
            end = None
            for td in all_td:
                if td.get_text(strip=True).casefold() == "start time".casefold():
                    start = extract_kattis_date(next(all_td).get_text(strip=True))
                if td.get_text(strip=True).casefold() == "end time".casefold():
                    end = extract_kattis_date(next(all_td).get_text(strip=True))
            yield (Assignment(
                url=url, assignment_id=url[url.rindex('/') + 1:], title=a.getText(),
                description=description, start=start, end=end
            ))


@top.command()
@click.argument("offering", default="")
def list_assignments(offering):
    """
    list the assignments for the given offering.
    :param offering: a substring of the offering name
    """
    load_config()
    for offering in get_offerings(offering):
        for assignment in get_assignments(offering):
            info(
                f"{assignment.title}: {assignment.start} to {assignment.end} {assignment.description} {assignment.url}")


@top.command()
@click.argument("offering")
@click.argument("assignment")
def download_submissions(offering, assignment):
    """
    download the submissions for an assignment in an offering. offerings and assignments that have the given substring
    will match.
    """
    load_config()
    for o in get_offerings(offering):
        for a in get_assignments(o):
            if assignment in a.title:
                for student, probs in get_best_submissions(o, a.assignment_id).items():
                    for problem, submission in probs.items():
                       base_path = f"{offering}/{assignment}/{problem}/{student}"
                       os.makedirs(base_path, exist_ok=True)
                       rsp, name = download_submission(submission.url)
                       with open(base_path + "/" + name, "wb") as f:
                           f.write(rsp.content)


def download_submission(url):
    rsp = web_get(f"https://{config.kattis_hostname}{url}")
    bs = BeautifulSoup(rsp.content, 'html.parser')
    src_div = bs.find(class_="file_source-content-file", recursive=True)
    a = src_div.find("a", recursive=True)
    h3 = src_div.find("h3")
    name = os.path.basename(h3.get_text().strip())
    sanitize(name)
    return web_get(f"https://{config.kattis_hostname}{a.get('href')}"), name


def sanitize(name):
    return re.sub(r"[^\w.]", "_", name)

def get_course(canvas, name, is_active=True) -> Course:
    """ find one course based on partial match """
    course_list = get_courses(canvas, name, is_active)
    if len(course_list) == 0:
        error(f'no courses found that contain {name}. options are:')
        for c in get_courses(canvas, "", is_active):
            error(fr"    {c.name}")
        sys.exit(2)
    elif len(course_list) > 1:
        error(f"multiple courses found for {name}:")
        for c in course_list:
            error(f"    {c.name}")
        sys.exit(2)
    return course_list[0]


def get_section(course: Course, section_name: str):
    """Find a section by name or unique substring. Returns the section or exits with error."""
    sections = list(course.get_sections())

    # First try exact match
    for section in sections:
        if section.name == section_name:
            return section

    # Try substring match
    matching = [s for s in sections if section_name in s.name]

    if len(matching) == 1:
        return matching[0]
    elif len(matching) > 1:
        error(f"multiple sections match '{section_name}':")
        for section in matching:
            error(f"    {section.name}")
        exit(6)
    else:
        error(f"section '{section_name}' not found in {course.name}. available sections:")
        for section in sections:
            error(f"    {section.name}")
        exit(6)


def get_courses(canvas: Canvas, name: str, is_active=True, is_finished=False) -> [Course]:
    """ find the courses based on partial match """
    courses = canvas.get_courses(enrollment_type="teacher")
    course_list = []
    for c in courses:
        start = c.start_at_date if hasattr(c, "start_at_date") else now
        end = c.end_at_date if hasattr(c, "end_at_date") else now
        if is_active and (start > now or end < now):
            continue
        if is_finished and end >= now:
            continue
        if name in c.name:
            c.start = start
            c.end = end
            course_list.append(c)
    return course_list


@top.command()
@click.argument("offering")
@click.argument("canvas_course")
@click.option("--dryrun/--no-dryrun", default=True, help="show planned actions, do not make them happen.")
@click.option("--force/--no-force", default=False, help="force an update of an assignment if it already exists.")
@click.option("--add-to-module", help="the module to add the assignment to.")
@click.option("--assignment-group", default="kattis", help="the canvas assignment group to use (default: kattis).")
@click.option("--section", help="only create assignments for this specific section.")
def course2canvas(offering, canvas_course, dryrun, force, add_to_module, assignment_group, section):
    """
    create assignments in canvas for all the assignments in kattis.
    """
    load_config()
    offerings = list(get_offerings(offering))
    if len(offerings) == 0:
        error(f"no offerings found for {offering}")
        exit(3)
    elif len(offerings) > 1:
        error(f"multiple offerings found for {offering}: {', '.join(offerings)}")
        exit(3)

    canvas = Canvas(config.canvas_url, config.canvas_token)
    course = get_course(canvas, canvas_course)

    # Get section if specified
    canvas_section = None
    if section:
        canvas_section = get_section(course, section)

    canvas_group = None
    available_groups = list(course.get_assignment_groups())
    for ag in available_groups:
        if ag.name == assignment_group:
            canvas_group = ag
            break
    if not canvas_group:
        if dryrun:
            error(f"assignment group '{assignment_group}' not found in {course.name}. available groups:")
            for ag in available_groups:
                error(f"    {ag.name}")
            error(f"use --no-dryrun to create the assignment group, or specify an existing group with --assignment-group.")
            exit(5)
        else:
            canvas_group = course.create_assignment_group(name=assignment_group)
            info(f"created assignment group '{assignment_group}'.")

    if add_to_module:
        modules = {m.name: m for m in course.get_modules()}
        if add_to_module in modules:
            add_to_module = modules[add_to_module]
        else:
            if dryrun:
                info(f"would create and publish {add_to_module}.")
            else:
                args = {"name": add_to_module}
                add_to_module = course.create_module(module=args)
                info(f"created module {add_to_module}.")

                args = {'published': "true"}
                add_to_module.edit(module=args)
                info(f"published module {add_to_module}.")

    # In dryrun mode without existing group, get all assignments; otherwise filter by group
    if canvas_group:
        canvas_assignments = {a.name: a for a in course.get_assignments(assignment_group_id=canvas_group.id)}
    else:
        canvas_assignments = {}

    # make sure assignments are in place
    sorted_assignments = list(get_assignments(offerings[0]))
    sorted_assignments.sort(key=lambda a: a.start)
    for assignment in sorted_assignments:
        description = assignment.description if assignment.description else ""

        # Base assignment data
        assignment_data = {
            'assignment_group_id': canvas_group.id,
            'name': assignment.title,
            'description': f'Solve the problems found at <a href="{assignment.url}" target="kattis-details">{assignment.url}</a>. {description}',
            'points_possible': 100,
            'published': True,
        }

        # If section specified, use assignment overrides instead of base dates
        if canvas_section:
            assignment_data['only_visible_to_overrides'] = True
            assignment_data['assignment_overrides'] = [{
                'course_section_id': canvas_section.id,
                'due_at': assignment.end,
                'lock_at': assignment.end,
                'unlock_at': assignment.start,
            }]
        else:
            assignment_data['due_at'] = assignment.end
            assignment_data['lock_at'] = assignment.end
            assignment_data['unlock_at'] = assignment.start

        if assignment.title in canvas_assignments:
            info(f"{assignment.title} already exists.")
            if force:
                if dryrun:
                    info(f"would update {assignment.title}.")
                else:
                    canvas_assignments[assignment.title].edit(assignment=assignment_data)
                    info(f"updated {assignment.title}.")
        else:
            if dryrun:
                section_info = f" (section: {section})" if canvas_section else ""
                info(f"would create {assignment}{section_info}")
            elif 'late' in assignment.title and assignment.title.replace("-late", "") in canvas_assignments:
                info(f"no new assignment created as --late assignment for {assignment.title.replace('-late', '')}.")
                continue
            else:
                canvas_assignments[assignment.title] = course.create_assignment(assignment_data)
                section_info = f" for section {section}" if canvas_section else ""
                info(f"created {assignment.title}{section_info}.")
        if add_to_module:
            if assignment.title not in [i.title for i in add_to_module.get_module_items()]:
                add_to_module.create_module_item(module_item={
                    'title': assignment.title,
                    'type': 'Assignment',
                    'content_id': canvas_assignments[assignment.title].id,
                })
                info(f'{assignment.title} added to {add_to_module.name}')
            else:
                info(f'{assignment.title} already in {add_to_module.name}')


def is_student_enrollment(user: User):
    return "StudentEnrollment" in [e['type'] for e in user.enrollments]


def find_kattis_link(profile: dict) -> str:
    kattis_url = None
    for link in profile["links"]:
        if "kattis" in link["title"].lower():
            kattis_url = link["url"]
    return kattis_url


class KattisLink(NamedTuple):
    canvas_user: User
    kattis_user: str


def get_kattis_links(course: Course, section_id: int = None) -> [KattisLink]:
    # this is so terribly slow because of all the requests, we need threads
    with concurrent.futures.ThreadPoolExecutor(max_workers=20) as executor:
        futures = []
        for u in course.get_users(include=["enrollments"]):
            # Check for student enrollment
            student_enrollments = [e for e in u.enrollments if e['type'] == "StudentEnrollment"]
            if not student_enrollments:
                continue

            # If section specified, filter by section
            if section_id:
                if not any(e.get('course_section_id') == section_id for e in student_enrollments):
                    continue

            def get_profile(user: User) -> Optional[KattisLink]:
                profile = user.get_profile(include=["links"])
                kattis_url = find_kattis_link(profile)
                kattis_url = extract_last(kattis_url) if kattis_url else None
                return KattisLink(canvas_user=user, kattis_user=kattis_url)

            futures.append(executor.submit(get_profile, u))

        links = [f.result() for f in futures if not None]
        links.sort(key=lambda l: l.canvas_user.name)
        return links


@top.command()
@click.argument("canvas_course")
def kattislinks(canvas_course):
    """
    list the students in the class with their email and kattis links.
    """
    load_config()
    canvas = Canvas(config.canvas_url, config.canvas_token)
    course = get_course(canvas, canvas_course)

    for link in get_kattis_links(course):
        if not is_student_enrollment(link.canvas_user):
            continue
        if link.kattis_user:
            info(f"{link.canvas_user.name}\t{link.canvas_user.email}\t{link.kattis_user}")
        else:
            error(f"{link.canvas_user.name}\t{link.canvas_user.email} missing kattis link")


@top.command()
@click.argument("offering")
@click.argument("canvas_course")
@click.option("--dryrun/--no-dryrun", default=True, help="show planned actions, do not make them happen.")
@click.option("--assignment-group", default="kattis", help="the canvas assignment group to use (default: kattis).")
@click.option("--section", help="only process submissions for students in this specific section.")
@click.option("--force-comment/--no-force-comment", default=False, help="add a comment about the best submission even if there is already a comment in canvas.")
def submissions2canvas(offering, canvas_course, dryrun, assignment_group, section, force_comment):
    """
    mirror summary of submission from kattis into canvas as a submission comment.
    """
    print(force_comment)
    load_config()
    offerings = list(get_offerings(offering))
    if len(offerings) == 0:
        error(f"no offerings found for {offering}")
        exit(3)
    elif len(offerings) > 1:
        error(f"multiple offerings found for {offering}: {', '.join(offerings)}")
        exit(3)

    canvas = Canvas(config.canvas_url, config.canvas_token)
    course = get_course(canvas, canvas_course)

    # Get section if specified
    canvas_section = None
    section_id = None
    if section:
        canvas_section = get_section(course, section)
        section_id = canvas_section.id
        info(f"filtering students by section: {section}")

    kattis_user2canvas_id = {}
    canvas_id2kattis_user = {}
    for link in get_kattis_links(course, section_id=section_id):
        if link.kattis_user:
            kattis_user2canvas_id[link.kattis_user] = link.canvas_user
            canvas_id2kattis_user[link.canvas_user.id] = link.kattis_user
        else:
            warn(f"kattis link missing for {link.canvas_user.name} {link.canvas_user.email}.")

    canvas_group = None
    for ag in course.get_assignment_groups():
        if ag.name == assignment_group:
            canvas_group = ag
            break

    if not canvas_group:
        error(f"no '{assignment_group}' assignment group in {canvas_course}")
        exit(4)

    assignments = {a.name: a for a in course.get_assignments(assignment_group_id=canvas_group.id)}

    for assignment in get_assignments(offerings[0]):
        if assignment.title.replace("-late", "") not in assignments:
            error(f"{assignment.title.replace('-late', '')} not in canvas {canvas_course}")
        else:
            prefix = "LATE: " if "late" in assignment.title else ""
            best_submissions = get_best_submissions(offering=offerings[0],
                                                    assignment_id=assignment.assignment_id)
            canvas_assignment = assignments[assignment.title.replace("-late", "")]
            # find the last submissions and only add a submission if the best submission is after latest
            submissions_by_user = {}
            for canvas_submission in canvas_assignment.get_submissions(include=["submission_comments"]):
                if canvas_submission.user_id in canvas_id2kattis_user:
                    if canvas_submission.user_id in submissions_by_user:
                        warn(
                            f'duplicate submission for {kattis_user2canvas_id[canvas_submission.user_id]} in {assignment.title}')
                    submissions_by_user[canvas_id2kattis_user[canvas_submission.user_id]] = canvas_submission
                    last_comment = datetime.datetime.fromordinal(1).replace(tzinfo=datetime.timezone.utc)
                    last_comment_text = ''
                    if canvas_submission.submission_comments:
                        for comment in canvas_submission.submission_comments:
                            created_at = extract_canvas_date(comment['created_at'])
                            if config.kattis_hostname in comment.get('comment', '') and created_at > last_comment:
                                last_comment = created_at
                                last_comment_text = comment.get('comment', '')
                    canvas_submission.last_comment = last_comment
                    canvas_submission.last_comment_text = last_comment_text
            for user, best in best_submissions.items():
                for kattis_submission in best.values():
                    if user not in submissions_by_user:
                        warn(f"i don't see a canvas user for {user}")
                    elif user not in kattis_user2canvas_id:
                        warn(f'skipping submission for unknown user {user}')
                    elif kattis_submission.date > submissions_by_user[user].last_comment or force_comment:
                        if dryrun:
                            warn(
                                f"would update {kattis_user2canvas_id[kattis_submission.user]} on problem {kattis_submission.problem} scored {kattis_submission.score}")
                        else:
                            href_url = f"https://{config.kattis_hostname}{kattis_submission.url}"
                            submissions_by_user[user].edit(comment={
                                'text_comment': f"{prefix}Submission <a href={href_url}>{href_url}</a> scored {kattis_submission.score} on {kattis_submission.problem}."})
                            info(
                                f"updated {submissions_by_user[user]} {kattis_user2canvas_id[kattis_submission.user]} for {assignment.title}")
                    else:
                        info(f"{user} up to date {kattis_submission.date} > {submissions_by_user[user].last_comment} {submissions_by_user[user].last_comment_text} ")


def get_best_submissions(offering: str, assignment_id: str) -> {str: {str: Submission}}:
    best_submissions = collections.defaultdict(dict)
    base_url = f"https://{config.kattis_hostname}{offering}/assignments/{assignment_id}/submissions"
    headers = None
    page = 0

    while True:
        url = f"{base_url}?page={page}"
        rsp = web_get(url)
        bs = BeautifulSoup(rsp.content, "html.parser")
        judge_table = bs.find("table", id="judge_table")

        if not judge_table:
            if page == 0:
                info(f"no submissions yet for {assignment_id}")
            break

        if headers is None:
            headers = [x.get_text().strip() for x in judge_table.find_all("th")]

        tbody = judge_table.find("tbody")
        rows = [r for r in tbody.find_all("tr", recursive=False) if r.get("data-submission-id")]

        if not rows:
            break

        for row in rows:
            cells = row.find_all("td", recursive=False)
            if not cells:
                continue
            props = {}
            for index, td in enumerate(cells):
                a = td.find("a")
                props[headers[index]] = a.get("href") if a else td.get_text().strip()
            date = props["Date"]
            if "-" in date:
                date = datetime.datetime.strptime(date, "%Y-%m-%d %H:%M:%S").replace(tzinfo=now.tzinfo)
            else:
                hms = datetime.datetime.strptime(date, "%H:%M:%S")
                date = now.replace(hour=hms.hour, minute=hms.minute, second=hms.second)
                # it's not clear when the short date version is used. it might be used when it is less than 24 hours,
                # in which case, just setting the time will make the date 24 hours more than it should be
                if date > now:
                    date -= datetime.timedelta(days=1)

            score = 0.0 if props["Test cases"] == "-/-" else float(Fraction(props["Test cases"])) * 100
            submission = Submission(user=extract_last(props["User"]), problem=extract_last(props["Problem"]), date=date,
                                    score=score, url=props[""])
            if submission.problem not in best_submissions[submission.user]:
                best_submissions[submission.user][submission.problem] = submission
            else:
                current_best = best_submissions[submission.user][submission.problem]
                if current_best.score < submission.score or (
                        current_best.score == submission.score and current_best.date < submission.date):
                    best_submissions[submission.user][submission.problem] = submission

        page += 1

    return best_submissions


@top.command()
@click.argument("canvas_course")
def sendemail(canvas_course):
    """
    Email students if they don't have a kattis link in their profile.
    It takes one input argument canvas course name.
    """
    load_config()
    canvas = Canvas(config.canvas_url, config.canvas_token)
    course = get_course(canvas, canvas_course)

    for link in get_kattis_links(course):
        if not is_student_enrollment(link.canvas_user):
            continue
        if not link.kattis_user:
            canvas.create_conversation(recipients=link.canvas_user.id,
                                       body="Hello " + link.canvas_user.name + "\n\n\n Please add the missing kattis "
                                                                               "link in bio for "
                                                                               "course " + canvas_course + ".",
                                       subject='Reminder: Add kattis link in profile')
            info(f"Able to send conversation to : {link.canvas_user.id}")


if __name__ == "__main__":
    top()
