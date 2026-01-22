# kattis2canvas

this is a simple python tool that uses the canvasapi toolkit to integrate a kattis offering with a canvas course. the tool was specifically made to work with the commercial kattis (as you will see in a moment). it would take a bit of tweaking on the web scraping to make it work with open.kattis.com.

the kattis connection is done using web scraping and thus it is very fragile! at the end, i will highlight where it is most vulnerable.

# setting up kattis2canvas

## the config file

first you will need to set up the config file. it has all your authorization tokens so DON'T CHECK IT IN. i specificially look for it in the app_dir as defined by click to get it far away from source. on linux this ends up being a file called ~/.config/kattis2canvas.ini. this is how it should be populated:

```
[kattis]
username: YOUR_KATTIS_USERNAME_NOT_EMAIL
token: SOME_RANDOM_CHARACTERS
hostname: THE_DOMAIN_NAME_OF_YOUR_KATTIS_INSTANCE
loginurl: THE_URL_TO_LOG_IN_TO_KATTIS

[canvas]
url=URL_OF_YOUR_CANVAS_INSTANCE
token=SOME_RANDOM_CHARACTERS
```

you can easily get the kattis section by going to https://\<kattis>/download/kattisrc where \<kattis> is your instance of kattis. you will need to move the lines around slightly. for canvas the url is the one you use to access the main page of canvas. you generate the token in the bottom of the Account -> Settings page.

you can check that everything is set up by running:

```
kattis2canvas list-offerings
kattis2canvas list-assignments
```

or if you built the pyz file using make_zipapp.sh

```
kattis2canvas list-offerings
kattis2canvas list-assignments
```

## mapping student kattis accounts to canvas accounts

in canvas, you can associate various URLs with your account in the Links section of Account -> Profile. students need to put the URL of their kattis account in a link with the word "kattis" (in any case) in the title. this ends up being the join key for kattis2canvas.

you can check if the students have set up the links properly using

```
kattis2canvas kattislinks
```

# using kattis2canvas

## populating kattis assignments in canvas

when you create new kattis assignments, you will need to get it mirrored into canvas. currently the **course2canvas** command will put all of the assignments into an assignment group called kattis. the assignment group must be created in your course before **course2canvas**.

when you specify the names of offerings in kattis and courses in canvas, you can specify a substring of the name and **kattis2canvas** will be able to use it as long as it matches exactly one name. if it doesn't, it will show it what it found.

when creating the assignment in canvas, anything you have put in the description in kattis will also be replicated in canvas along with a link to the kattis assignment.

if you have made changes to a kattis assignment that you have already populated in canvas, use the --force option to force an update. (right now there isn't a way to force individual assignments.)

if you use modules, you can use the **--add-to-module** flag to add the kattis assignments to a module. at this point, it puts all of the kattis assignments into that one module.

## getting the results to canvas

the **submissions2canvas** will replicate results from student submissions to kattis into canvas. it will only replicate results that are either better or the same as or newer than previous results it has replicated. a summary of the problem, score, and link to the submission will be added as a comment for the student in the gradebook for the relevant assignment. the idea is that when it's time to grade, you have easy access to the results and the link to the source from canvas speedgrader.

# kattis webscraping

unfortuately, scraping the kattis API is very adhoc and it would be naive to think that it wouldn't change in ways that will break this tool. we use BeautifulSoupe (it's pretty beautiful...) so here are the features that the script relies on for kattis webpages: (note the term "assume" is used for things we have to believe because that is the only reasonable way that we can use the information we are given.)

* the list of offerings: (HOSTNAME is from the config file above) we assume http://HOSTNAME/ will give us a page will all the offerings and the urls in the href of those offerings will have the form **/courses/[^/]+/[^/]+**
* the list of assignments: we assume the offering page will have the detail page for assignments in hrefs of anchor tabs of the form "assignments/\w+$".
* the assignment details: we assume the assignment detail page will have an \<h2> tag with the text "Description" followed by a sibling \<p> tag that entirely contains the description. we also assume that there will be a \<td> for "start time" and another for "end time" we do case insensitve comparisons to find them. we assume that the following \<td> tage will have the time.
* time: TIME IS HARD! if the time is recent, kattis will drop the date, so if we get a time with no date, we take the current time and up date the HH:MM:SS with the date we get from kattis. we also assume all dates are UTC.
* getting submissions: we assume the submissions for an assignment are found at https://HOSTNAME/OFFERING/assignments/ASSIGNMENT_ID/submissions it appears that all submissions for the assignment are listed on that page. submissions for a problem outside of the assignment time period will not show up on that page. the submissions are in a table called "judege"table". the headers \<th> that we are looking for are "User" for the user url, "Problem" for the name of the problem, "Test cases" for the score reflected as sucess/count with -/- indicating no tries, and "" indicating the header for the url of the submission. once we know the column numbers we want, we have to look for the \<tbody> child of the table (problems happen if you try to look for \<td> recursively from the table!) then we look for \<tr> children of \<tbody> which have a "data-submission-id" attribute.


