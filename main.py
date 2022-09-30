import collections
import configparser
import datetime
import logging
import re
import sys
from collections import namedtuple
from fractions import Fraction
from typing import NamedTuple, Optional

import click
import requests
import requests.exceptions
from bs4 import BeautifulSoup
from canvasapi import (Canvas)
from canvasapi.course import Course

HEADERS = {'User-Agent': 'kattis-to-canvas'}

Config = namedtuple(
    "Config",
    "kattis_username kattis_token kattis_loginurl kattis_hostname canvas_url canvas_token",
)
config: Optional[Config] = None
login_cookies = None

Student = namedtuple("Student", "kattis_url name email canvas_id")


class Submission(NamedTuple):
    user: str
    problem: str
    score: float
    url: str
    date: datetime.datetime


now = datetime.datetime.now(datetime.timezone.utc)


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


def extract_last(pathish: str):
    last_slash = pathish.rindex("/")
    if last_slash:
        pathish = pathish[last_slash+1:]
    return pathish


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
            yield h


@top.command()
@click.argument("offering", default="")
def list_offerings(offering):
    """
    list the possible offerings.
    :param offering: a substring of the offering name
    """

    for offering in get_offerings(offering):
        info(offering)


def extract_kattis_date(element):
    return datetime.datetime.strftime(datetime.datetime.strptime(element, "%Y-%m-%d %H:%M %Z"), "%Y-%m-%dT%H:%M:00%z")


def extract_canvas_date(element):
    return datetime.datetime.strptime(element, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)


Assignment = namedtuple("Assignment", "url assignment_id title description start end")


def get_assignments(offering):
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
    for offering in get_offerings(offering):
        for assignment in get_assignments(offering):
            info(
                f"{assignment.title}: {assignment.start} to {assignment.end} {assignment.description} {assignment.url}")


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
def course2canvas(offering, canvas_course, dryrun, force):
    """
    create assignments in canvas for all the assignments in kattis.
    """
    offerings = list(get_offerings(offering))
    if len(offerings) == 0:
        error(f"no offerings found for {offering}")
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
        error(f"no kattis assignment group in {canvas_course}")
        exit(4)

    canvas_assignments = {a.name: a for a in course.get_assignments(assignment_group_id=kattis_group.id)}

    # make sure assignments are in place
    for assignment in get_assignments(offerings[0]):
        description = assignment.description if assignment.description else ""
        if assignment.title in canvas_assignments:
            info(f"{assignment.title} already exists.")
            if force:
                if dryrun:
                    info(f"would update {assignment.title}.")
                else:
                    print(canvas_assignments[assignment.title])
                    canvas_assignments[assignment.title].edit(assignment={
                        'assignment_group_id': kattis_group.id,
                        'name': assignment.title,
                        'description': f'Solve the problems found at <a href="{assignment.url}">{assignment.url}</a>. {description}',
                        'points_possible': 100,
                        'due_at': assignment.end,
                        'lock_at': assignment.end,
                        'unlock_at': assignment.start,
                    })
        else:
            if dryrun:
                info(f"would create {assignment}")
            else:
                course.create_assignment({
                    'assignment_group_id': kattis_group.id,
                    'name': assignment.title,
                    'description': f'Solve the problems found at <a href="{assignment.url}">{assignment.url}</a>. {description}',
                    'points_possible': 100,
                    'due_at': assignment.end,
                    'lock_at': assignment.end,
                    'unlock_at': assignment.start,
                })
                info(f"created {assignment.title}.")


@top.command()
@click.argument("canvas_course")
def kattislinks(canvas_course):
    """
    list the students in the class with their email and kattis links.
    """
    canvas = Canvas(config.canvas_url, config.canvas_token)
    course = get_course(canvas, canvas_course)

    for e in course.get_enrollments():
        if e.type != "StudentEnrollment":
            continue
        user = course.get_user(e.user_id)
        profile = user.get_profile(include=["links"])
        kattis_url = find_kattis_link(profile)
        print(f"{profile['name']}\t{profile['primary_email']}\t{kattis_url}")


def find_kattis_link(profile):
    kattis_url = None
    for link in profile["links"]:
        if "kattis" in link["title"].lower():
            kattis_url = link["url"]
    return kattis_url


@top.command()
@click.argument("offering")
@click.argument("canvas_course")
@click.option("--dryrun/--no-dryrun", default=True, help="show planned actions, do not make them happen.")
def submissions2canvas(offering, canvas_course, dryrun):
    """
    mirror summary of submission from kattis into canvas as a submission comment.
    """
    offerings = list(get_offerings(offering))
    if len(offerings) == 0:
        error(f"no offerings found for {offering}")
        exit(3)
    elif len(offerings) > 1:
        error(f"multiple offerings found for {offering}: {', '.join(offerings)}")
        exit(3)

    canvas = Canvas(config.canvas_url, config.canvas_token)
    course = get_course(canvas, canvas_course)

    kattis_user2canvas_id = {}
    canvas_id2kattis_user = {}
    for e in course.get_enrollments():
        if e.type != "StudentEnrollment":
            continue
        user = course.get_user(e.user_id)
        profile = user.get_profile(include=["links"])
        kattis_url = find_kattis_link(profile)
        kattis_url = extract_last(kattis_url) if kattis_url else None

        if kattis_url:
            kattis_user2canvas_id[kattis_url] = user
            canvas_id2kattis_user[user.id] = kattis_url
        else:
            warn(f"kattis link missing for {user.name}.")

    kattis_group = None
    for ag in course.get_assignment_groups():
        if ag.name == 'kattis':
            kattis_group = ag
            break

    if not kattis_group:
        error(f"no kattis assignment group in {canvas_course}")
        exit(4)

    assignments = {a.name: a for a in course.get_assignments(assignment_group_id=kattis_group.id)}

    for assignment in get_assignments(offerings[0]):
        if assignment.title not in assignments:
            error(f"{assignment.title} not in canvas {canvas_course}")
        else:
            best_submissions = get_best_submissions(offering=offerings[0],
                                                    assignment_id=assignment.assignment_id)
            canvas_assignment = assignments[assignment.title]
            # find the last submissions and only add a submission if the best submission is after latest
            submissions_by_user = {}
            for canvas_submission in canvas_assignment.get_submissions(include=["submission_comments"]):
                if canvas_submission.user_id in canvas_id2kattis_user:
                    if canvas_submission.user_id in submissions_by_user:
                        warn(f'duplicate submission for {kattis_user2canvas_id[canvas_submission.user_id]} in {assignment.title}')
                    submissions_by_user[canvas_id2kattis_user[canvas_submission.user_id]] = canvas_submission
                    last_comment = datetime.datetime.fromordinal(1).replace(tzinfo=datetime.timezone.utc)
                    if canvas_submission.submission_comments:
                        for comment in canvas_submission.submission_comments:
                            created_at = extract_canvas_date(comment['created_at'])
                            if created_at > last_comment:
                                last_comment = created_at
                    canvas_submission.last_comment = last_comment

            for user, best in best_submissions.items():
                for kattis_submission in best.values():
                    if user not in submissions_by_user:
                        warn(f"i don't see a canvas submission for {user}")
                    elif user not in kattis_user2canvas_id:
                        warn(f'skipping submission for unknown user {user}')
                    elif kattis_submission.date > submissions_by_user[user].last_comment:
                        if dryrun:
                            warn(
                                f"would update {kattis_user2canvas_id[kattis_submission.user]} on problem {kattis_submission.problem} scored {kattis_submission.score}")
                        else:
                            submissions_by_user[user].edit(comment={'text_comment': f"Submission https://{config.kattis_hostname}{kattis_submission.url} scored {kattis_submission.score} on {kattis_submission.problem}."})
                            info(f"updated {submissions_by_user[user]} {kattis_user2canvas_id[kattis_submission.user]} for {assignment.title}")
                    else:
                        info(f"{user} up to date")


def get_best_submissions(offering, assignment_id):
    best_submissions = collections.defaultdict(dict)
    rsp = web_get(f"https://{config.kattis_hostname}{offering}/assignments/{assignment_id}/submissions")
    bs = BeautifulSoup(rsp.content, "html.parser")
    judge_table = bs.find("table", id="judge_table")
    headers = [x.get_text().strip() for x in judge_table.find_all("th")]
    tbody = judge_table.find("tbody")
    for submissions in tbody.find_all("tr", recursive=False):
        if not submissions.get("data-submission-id"):
            continue
        submissions = submissions.find_all("td", recursive=False)
        if not submissions:
            continue
        props = {}
        for index, td in enumerate(submissions):
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
            best_submissions[submission.user] = {submission.problem: submission}
        else:
            current_best = best_submissions[submission.user][submission.problem]
            if current_best.score < submission.score or (
                    current_best.score == submission.score and current_best.date < submission.date):
                best_submissions[submission.user][submission.problem] = submission
    return best_submissions


if __name__ == "__main__":
    top()
