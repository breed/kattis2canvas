import configparser
import datetime
import re
import sys
from collections import namedtuple

import click
import requests
import requests.exceptions
from bs4 import BeautifulSoup
from canvasapi import Canvas
from canvasapi.course import Course


def error(message):
    click.echo(message)


HEADERS = {'User-Agent': 'kattis-to-canvas'}

Config = namedtuple(
    "Config",
    "kattis_username kattis_token kattis_loginurl kattis_hostname canvas_url canvas_token",
)
config = None
login_cookies = None

Student = namedtuple("Student", "kattis_url name email canvas_id")


def error(message):
    click.echo(click.style(message, fg='red'))


def info(message):
    click.echo(click.style(message, fg='blue'))


def warn(message):
    click.echo(click.style(message, fg='yellow'))


def check_status(rsp):
    if rsp.status_code != 200:
        error(f"got status {rsp.status_code} for {rsp.url}.")
        exit(6)

def introspect(o):
    print("class", o.__class__)
    for i in dir(o):
        print(i)

def web_get(url):
    rsp = requests.get(url, cookies=login_cookies, headers=HEADERS)
    check_status(rsp)
    return rsp

@click.group()
def top():
    config_ini = click.get_app_dir("kattis2canvas.ini")
    parser = configparser.ConfigParser()
    parser.read([config_ini])
    global config
    config = Config(
        kattis_username=parser['kattis']['username'],
        kattis_token=parser['kattis']['token'],
        kattis_hostname=parser['kattis']['hostname'],
        kattis_loginurl=parser['kattis']['loginurl'],
        canvas_url=parser['canvas']['url'],
        canvas_token=parser['canvas']['token'],
    )
    global login_cookies
    login_cookies = login()


def login():
    args = {'user': config.kattis_username, 'script': 'true', 'token': config.kattis_token}
    rsp = requests.post(config.kattis_loginurl, data=args, headers=HEADERS)
    if rsp.status_code != 200:
        error(f"Kattis login failed. Status: {rsp.status_code}")
        exit(2)
    return rsp.cookies


def get_offerings(offering_pattern):
    rsp = web_get(f"https://{config.kattis_hostname}/")
    bs = BeautifulSoup(rsp.content, 'html.parser')
    for a in bs.find_all('a'):
        h = a.get('href')
        if h and re.match("/courses/[^/]+/[^/]+", h) and offering_pattern in h:
            yield (h)


@top.command()
@click.argument("offering", default="")
def list_offerings(offering):
    """
    list the possible offerings.
    :param offering: a substring of the offering name
    """

    for offering in get_offerings(offering):
        info(offering)


def extract_date(element):
    return datetime.datetime.strftime(datetime.datetime.strptime(element, "%Y-%m-%d %H:%M %Z"), "%Y-%m-%dT%H:%M:00%z")


Problem = namedtuple("Problem", "url title start end")


def get_problems(offering):
    rsp = web_get(f"https://{config.kattis_hostname}{offering}")
    bs = BeautifulSoup(rsp.content, 'html.parser')
    for a in bs.find_all('a'):
        h = a.get('href')
        if h and h.endswith("/problems"):
            url = h.removesuffix("/problems")
            rsp2 = web_get(f"https://{config.kattis_hostname}{url}")
            bs2 = BeautifulSoup(rsp2.content, 'html.parser')
            all = iter(bs2.find_all("td"))
            for td in all:
                if td.get_text(strip=True).casefold() == "start time".casefold():
                    start = extract_date(next(all).get_text(strip=True))
                if td.get_text(strip=True).casefold() == "end time".casefold():
                    end = extract_date(next(all).get_text(strip=True))
            yield (Problem(url=url, title=a.getText(), start=start, end=end))


@top.command()
@click.argument("offering", default="")
def list_problems(offering):
    """
    list the problems for the given offering.
    :param offering: a substring of the offering name
    """
    for offering in get_offerings(offering):
        for problem in get_problems(offering):
            info(problem)


def get_course(canvas, name, is_active=True) -> Course:
    ''' find one course based on partial match '''
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


def get_courses(canvas: Canvas, name: str, is_active=True, is_finished=False) -> [Course]:
    ''' find the courses based on partial match '''
    courses = canvas.get_courses(enrollment_type="teacher")
    now = datetime.datetime.now(datetime.timezone.utc)
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
def course2canvas(offering, canvas_course, dryrun):
    offerings = list(get_offerings(offering))
    if len(offerings) == 0:
        error(f"no offerings found for {offering}");
        exit(3)
    elif len(offerings) > 1:
        error(f"multiple offerings found for {offering}: {', '.join(offerings)}")
        exit(3)

    canvas = Canvas(config.canvas_url, config.canvas_token)
    course = get_course(canvas, canvas_course)

    kattis_group = None
    for ag in course.get_assignment_groups():
        if ag.name == 'kattis':
            kattis_group = ag
            break

    if not kattis_group:
        error(f"no kattis assignment group in {canvas_course}");
        exit(4)

    assignments = [a.name for a in course.get_assignments(assignment_group_id=kattis_group.id)]

    # make sure assignments are in place
    for problem in get_problems(offerings[0]):
        if problem.title in assignments:
            info(f"{problem.title} already exists.")
        else:
            if dryrun:
                info(f"would create {problem}")
            else:
                full_url = f"https://{config.kattis_hostname}{problem.url}"

                course.create_assignment({
                    'assignment_group_id': kattis_group.id,
                    'name': problem.title,
                    'description': f'Solve the problems found at <a href="{full_url}">{full_url}</a>.',
                    'due_at': problem.end,
                    'lock_at': problem.end,
                    'unlock_at': problem.start,
                })
                info(f"created {problem.title}.")


@top.command()
@click.argument("offering")
@click.argument("canvas_course")
@click.option("--dryrun/--no-dryrun", default=True, help="show planned actions, do not make them happen.")
def submissions2canvas(offering, canvas_course, dryrun):
    offerings = list(get_offerings(offering))
    if len(offerings) == 0:
        error(f"no offerings found for {offering}");
        exit(3)
    elif len(offerings) > 1:
        error(f"multiple offerings found for {offering}: {', '.join(offerings)}")
        exit(3)

    canvas = Canvas(config.canvas_url, config.canvas_token)
    course = get_course(canvas, canvas_course)

    users = {}
    for e in course.get_enrollments():
        if e.type != "StudentEnrollment":
            continue
        user = course.get_user(e.user_id)
        profile = user.get_profile(include=["links"])
        kattis_url = None
        if "links" in profile:
            for link in profile["links"]:
                if link["title"] == "kattis":
                    kattis_url = link["url"]
                    break

        if kattis_url:
            users[kattis_url] = user
        else:
            warn(f"kattis link missing for {user.name}.")

    kattis_group = None
    for ag in course.get_assignment_groups():
        if ag.name == 'kattis':
            kattis_group = ag
            break

    if not kattis_group:
        error(f"no kattis assignment group in {canvas_course}");
        exit(4)

    assignments = {a.name: a.id for a in course.get_assignments(assignment_group_id=kattis_group.id)}

    map = {}
    # make sure assignments are in place
    for problem in get_problems(offerings[0]):
        if problem.title in assignments:
            map[problem.title] = assignments[problem.title]
        else:
            warn(f"can't find assignment for {problem.title}")

    rsp = web_get(f"https://{config.kattis_hostname}{offerings[0]}/submissions")
    bs = BeautifulSoup(rsp.content, "html.parser")
    judge_table = bs.find("table", id="judge_table")
    for submission in judge_table.find_all("tr"):

        print("*** start of tr")
        for index, td in enumerate(submission):
            print("\t", index, td)

if __name__ == "__main__":
    top()
