# This file is part of OpenHatch.
# Copyright (C) 2010 Parker Phinney
# Copyright (C) 2009 Karen Rustad
# Copyright (C) 2010 John Stumpo
# Copyright (C) 2011 Krzysztof Tarnowski (krzysztof.tarnowski@ymail.com)
# Copyright (C) 2009, 2010, 2011 OpenHatch, Inc.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

# Imports {{{

# Python
import StringIO
import datetime
from dateutil import parser
import urllib
import collections
import logging
import difflib

from django.utils import simplejson


# Django
from django.template.loader import render_to_string
from django.template import RequestContext
from django.core import serializers
from django.http import \
        HttpResponse, HttpResponseRedirect, HttpResponseServerError, HttpResponsePermanentRedirect, HttpResponseBadRequest, Http404
from django.shortcuts import get_object_or_404
from django.contrib.auth.models import User
from django.contrib.auth.decorators import login_required
from django.core.urlresolvers import reverse
from django.views.decorators.csrf import csrf_exempt
from django.utils.safestring import mark_safe
import django.views.generic
import csv
from mysite.libs import HTML
from xmlbuilder import XMLBuilder

# OpenHatch apps
import mysite.base.view_helpers
import mysite.base.unicode_sanity
from mysite.profile import view_helpers
import mysite.profile.view_helpers
from mysite.profile.models import \
        Person, Tag, TagType, \
        Link_Project_Tag, Link_Person_Tag, \
        DataImportAttempt, PortfolioEntry, Citation, FormResponse, FormQuestion, FormAnswer
from mysite.search.models import Project
from mysite.base.decorators import view, as_view, has_permissions, _has_permissions, _has_group
import mysite.profile.forms
import mysite.profile.tasks
from mysite.base.view_helpers import render_response
from django.views.decorators.csrf import csrf_protect
from mysite.profile.models import CardDisplayedQuestion
from mysite.profile.templatetags.profile_extras import get_card_fields_with_icons_together
import mysite.account.views
from mysite.settings import MEDIA_ROOT, MEDIA_URL

# }}}

@login_required
@csrf_protect
def delete_user_for_being_spammy(request):
    form = mysite.profile.forms.DeleteUser()
    if request.method == 'POST':
        if request.user.username != 'paulproteus':
            return HttpResponseBadRequest("Sorry, you may not do that.")
        form = mysite.profile.forms.DeleteUser(
            request.POST)
        if form.is_valid():
            u = User.objects.get(username=form.cleaned_data['username'])
            # Dump data about the user to the site admins
            mysite.profile.view_helpers.send_user_export_to_admins(u)
            # Send out an email to the poor sap.
            mysite.profile.view_helpers.email_spammy_user(u)
            # Okay... delete the user.
            u.delete() # hoo boy!
            return HttpResponseRedirect(reverse(
                    delete_user_for_being_spammy))

    return as_view(
        request,
        'profile/delete_user.html',
        {'form': form},
        None)

@login_required
def add_citation_manually_do(request):
    # {{{
    form = mysite.profile.forms.ManuallyAddACitationForm(request.POST)
    form.set_user(request.user)

    output = {
            'form_container_element_id': request.POST['form_container_element_id']
            }
    if form.is_valid():
        citation = form.save()

        # Manually added citations are published automatically.
        citation.is_published = True
        citation.save()

        json = simplejson.dumps(output)
        return HttpResponse(json, mimetype='application/json')

    else:
        error_msgs = []
        for error in form.errors.values():
            error_msgs.extend(eval(error.__repr__())) # don't ask questions.

        output['error_msgs'] = error_msgs
        json = simplejson.dumps(output)
        return HttpResponseServerError(json, mimetype='application/json')

    #}}}

@view
def display_person_web(request, user_to_display__id=None):
    # {{{

    user = get_object_or_404(User, pk=user_to_display__id)
    person, was_created = Person.objects.get_or_create(user=user)

    if person.private and request.user != user and _has_permissions(request.user,['can_view_people']) == False:
        raise Http404

    data = get_personal_data(person)
    all_responses = person.formresponse_set.all()
    data['questions'] = [{ 'question': question, 'responses': [response for response in all_responses
                                                               if response.question.id == question.id] }
                         for question in FormQuestion.objects.all() if question
                         in [response.question for response in all_responses]]
    data['edit_mode'] = False
    data['editable'] = (request.user == user)
    data['notifications'] = mysite.base.view_helpers.get_notification_from_request(request)
    data['explain_to_anonymous_users'] = True
    data['how_many_archived_pf_entries'] = person.get_published_portfolio_entries().filter(is_archived=True).count()

    if request.method == 'POST':
        projects_form = mysite.profile.forms.SelectProjectsForm(request.POST)
        if projects_form.is_valid():
            projects = projects_form.cleaned_data.get('Projects')
            delete_unselected_portfolio_entries(projects, person)
            save_selected_portfolio_entries(projects, person)

    data['projects_form'] = mysite.profile.forms.SelectProjectsForm(initial={
        'Projects': person.get_list_of_all_published_projects()
    })

    return (request, 'profile/main.html', data)

    # }}}

def delete_unselected_portfolio_entries(projects, person):
    portfolio_entries = person.get_published_portfolio_entries()
    for p in portfolio_entries:
        if p.project not in projects:
            p.delete()

def save_selected_portfolio_entries(projects, person):
    for project in projects:
        project_db = Project.objects.get(name=project.name)
        portfolio_entry, _ = PortfolioEntry.objects.get_or_create(project=project_db, person=person)
        portfolio_entry.is_published = True
        portfolio_entry.save()

#FIXME: Create a separate function that just passes the data required for displaying the little user bar on the top right to the template, and leaves out all the data required for displaying the large user bar on the left.
def get_personal_data(person):
    # {{{

    # FIXME: Make this more readable.
    data_dict = {
            'person': person,
            'photo_url': person.get_photo_url_or_default(),
            }

    data_dict['tags'] = tags_dict_for_person(person)
    data_dict['tags_flat'] = dict(
        [ (key, ', '.join([k.text for k in data_dict['tags'][key]]))
          for key in data_dict['tags'] ])

    data_dict['has_set_info'] = any(data_dict['tags_flat'].values())

    data_dict['contact_blurb'] = mysite.base.view_helpers.put_forwarder_in_contact_blurb_if_they_want(person.contact_blurb, person.user)

    data_dict['projects_i_wanna_help'] = person.projects_i_wanna_help.all()

    return data_dict

    # }}}

def tags_dict_for_person(person):
    # {{{
    ret = collections.defaultdict(list)
    links = Link_Person_Tag.objects.filter(person=person).order_by('id')
    for link in links:
        ret[link.tag.tag_type.name].append(link.tag)

    return ret
    # }}}

# FIXME: Test this.
def widget_display_undecorated(request, user_to_display__id):
    """We leave this function unwrapped by @view """
    """so it can referenced by widget_display_string."""
    # {{{
    user = get_object_or_404(User, id=user_to_display__id)
    person = get_object_or_404(Person, user=user)

    data = get_personal_data(person)
    data.update(mysite.base.view_helpers.get_uri_metadata_for_generating_absolute_links(
        request))
    return (request, 'profile/widget.html', data)
    # }}}

widget_display = view(widget_display_undecorated)

def widget_display_string(request, user_to_display__id):
    request, template, data = widget_display_undecorated(request, user_to_display__id)
    return render_to_string(template, data)

def widget_display_js(request, user_to_display__id):
    # FIXME: In the future, use:
    html_doc = widget_display_string(request, user_to_display__id)
    # to generate html_doc
    encoded_for_js = simplejson.dumps(html_doc)
    # Note: using application/javascript as suggested by
    # http://www.ietf.org/rfc/rfc4329.txt
    return render_response(request, 'base/append_ourselves.js',
                              {'in_string': encoded_for_js},
                              mimetype='application/javascript')

# }}}

# Debtags {{{

def add_one_debtag_to_project(project_name, tag_text):
    # {{{
    tag_type, created = TagType.objects.get_or_create(name='Debtags')

    project, project_created = Project.objects.get_or_create(name=project_name)

    tag, tag_created = Tag.objects.get_or_create(
            text=tag_text, tag_type=tag_type)

    new_link = Link_Project_Tag.objects.create(
            tag=tag, project=project,
            source='Debtags')
    new_link.save()
    return new_link
# }}}

def list_debtags_of_project(project_name):
    # {{{
    debtags_list = list(TagType.objects.filter(name='Debtags'))
    if debtags_list:
        debtags = debtags_list[0]
    else:
        return []

    project_list = list(Project.objects.filter(name=project_name))
    if project_list:
        project = project_list[0]
    else:
        return []

    resluts = list(Link_Project_Tag.objects.filter(project=project,
        tag__tag_type=debtags))
    return [link.tag.text for link in resluts]
    # }}}

def import_debtags(cooked_string = None):
    # {{{
    if cooked_string is None:
        # Warning: this re-downloads the list from Alioth every time this
        # is called
        import urllib2
        import gzip
        fd = urllib2.urlopen('http://debtags.alioth.debian.org/tags/tags-current.gz')
        gzipped_sio = StringIO.StringIO(fd.read()) # this sucks, but I
        # can't stream to
        # gzip.GzipFile because
        # urlopen()'s result
        # lacks tell()
        gunzipped = gzip.GzipFile(fileobj=gzipped_sio)
    else:
        gunzipped = StringIO.StringIO(cooked_string)
    for line in gunzipped:
        if ':' in line:
            package, tagstring = line.split(':', 1)
            tags = map(lambda s: s.strip(), tagstring.split(','))
            for tag in tags:
                add_one_debtag_to_project(package, tag)
    # }}}

# }}}

# Project experience tags {{{

# }}}

def _project_hash(project_name):
    # {{{
    # This prefix is a sha256 of 1MiB of /dev/urandom
    PREFIX = '_project_hash_2136870e40a759b56b9ba97a0'
    PREFIX += 'd7f60b84dbc90097a32da284306e871105d96cd'
    import hashlib
    hashed = hashlib.sha256(PREFIX + project_name)
    return hashed.hexdigest()
    # }}}

@login_required
# this is a post handler
def edit_person_info_do(request):
    # {{{
    person_id = request.POST.get(u'person_id')
    person = Person.objects.get(pk__exact=person_id)
    edit_info_form = mysite.profile.forms.EditInfoForm(request.POST, request.FILES, prefix='edit-tags', person=person)

    if edit_info_form.is_valid() != True:
        return edit_info(request,edit_info_form=edit_info_form, has_errors=True)

    FormResponse.objects.filter(person=person).delete()
    person.is_updated = True
    person.user.first_name=edit_info_form['first_name'].data
    person.user.last_name=edit_info_form['last_name'].data
    person.user.email=edit_info_form['email'].data
    person.user.save()
    person.save()
    for question in edit_info_form.questions:
        if edit_info_form['question_' + str(question.id)].data:
            if type(edit_info_form['question_' + str(question.id)].data) == list:
                for i, answer in enumerate(edit_info_form['question_' + str(question.id)].data):
                    mysite.profile.models.FormResponse(person=person, question=question, value=edit_info_form['question_' + str(question.id)].data[i]).save()
            elif type(edit_info_form['question_' + str(question.id)].data) == django.core.files.uploadedfile.InMemoryUploadedFile:
                file_from_form = edit_info_form['question_' + str(question.id)].data
                new_file_path = mysite.account.views.generate_random_file_path(file_from_form.name)
                with open(MEDIA_ROOT + '/' + new_file_path, "wb") as file:
                    file.write(file_from_form.read())
                    file.close()
                mysite.profile.models.FormResponse(person=person, question=question, value=new_file_path).save()
            else:
                mysite.profile.models.FormResponse(person=person, question=question, value=edit_info_form['question_' + str(question.id)].data).save()
    return HttpResponseRedirect(person.profile_url)

    # FIXME: This is racey. Only one of these functions should run at once.
    # }}}

@login_required
def ask_for_tag_input(request, username):
    # {{{
    return display_person_web(request, username, 'tags', edit='1')
    # }}}

def cut_list_of_people_in_three_columns(people):
    third = len(people)/3
    return [people[0:third], people[third:(third*2)], people[(third*2):]]

def cut_list_of_people_in_two_columns(people):
    half = len(people)/2
    return [people[0:half], people[half:]]

def permanent_redirect_to_people_search(request, property, value):
    '''Property is the "tag name", and "value" is the text in it.'''
    if property == 'seeking':
        property = 'can_pitch_in'

    if ' ' in value:
        escaped_value = '"' + value + '"'
    else:
        escaped_value = value

    q = '%s:%s' % (property, escaped_value)
    get_args = {u'q': q}
    destination_url = (reverse('mysite.profile.views.people') + '?' +
                       mysite.base.unicode_sanity.urlencode(get_args))
    return HttpResponsePermanentRedirect(destination_url)

def manyToString(many):
    res = ""
    for obj in many.all():
        res += obj.name + "; "
    return res

def prepare_person_row(person, questions_to_export, user_id, can_view_email):
    empty = "N/A"

    person_responses = get_card_fields_with_icons_together(person, user_id)

    person_row = [person.user.first_name,
            person.user.last_name]
    if (can_view_email):
        person_row.append(person.user.email)

    for field in questions_to_export:
        person_row.append(person_responses.get(field, empty))

    return person_row


def export_to_csv(people, questions_to_export, user_id, can_view_email):
    response = HttpResponse(content_type = "text/csv")
    response['Content-Disposition'] = 'attachment; filename="sc4g-people.csv"'
    writer = csv.writer(response)
    basic_fields = ['First name', 'Last name']
    if (can_view_email):
        basic_fields.append('E-mail')
    writer.writerow(basic_fields + questions_to_export)
    for person in people:
        writer.writerow(prepare_person_row(person, questions_to_export, user_id, can_view_email))
    return response

def export_to_json(people, questions_to_export, user_id, can_view_email):
    response = HttpResponse(content_type = "application/json")
    response['Content-Disposition'] = 'attachment; filename="sc4g-people.json"'
    to_export = []
    for person in people:
        to_export.append(get_all_person_fields(person, user_id, can_view_email))
    response.content = simplejson.dumps(to_export)
    return response

def get_all_person_fields(person, user_id, can_view_email):
    fields = get_card_fields_with_icons_together(person, user_id)
    fields['First name'] = person.user.first_name
    fields['Last name'] = person.user.last_name
    if (can_view_email):
        fields['E-mail'] = person.user.email
    return fields

def export_to_html(people, questions_to_export, user_id, can_view_email):
    response = HttpResponse(content_type = "text/html")
    response['Content-Disposition'] = 'attachment; filename="sc4g-people.html"'
    fields_to_export = ['First name', 'Last name']
    if (can_view_email):
        fields_to_export.append('E-mail')
    table = HTML.Table(header_row=fields_to_export + questions_to_export)
    for person in people:
        table.rows.append(prepare_person_row(person, questions_to_export, user_id, can_view_email))
    response.content = str(table)
    return response

def export_to_xml(people, questions_to_export, user_id, can_view_email):
    response = HttpResponse(content_type = "application/xml")
    response['Content-Disposition'] = 'attachment; filename="sc4g-people.xml"'
    xml = XMLBuilder('volunteers')
    for person in people:
        with xml.volunteer:
            xml.firstName(person.user.first_name)
            xml.lastName(person.user.last_name)
            if (can_view_email):
                xml.email(person.user.email)
            with xml.form_responses:
                for key, value in get_card_fields_with_icons_together(person, user_id).items():
                    xml.form_response(value, question=key)

    response.content = str(xml)
    return response

def people_export(request):
    query = request.POST.get('q', '')
    parsed_query = mysite.profile.view_helpers.parse_string_query(query)

    if parsed_query['q'].strip():
        people = parsed_query['callable_searcher']().people
    else:
        people = Person.objects.all().order_by('user__username')

    people = mysite.profile.view_helpers.filter_people(people, request.POST)

    if 'selected_people' in request.POST:
        selected_people = [int(id) for id in str(request.POST.get('selected_people')).split(',')]
        people = [person for person in people if person.id in [int(id) for id in selected_people]]

    if 'date_range' in request.POST:
        date_from = None
        date_to = None
        date_range = str(request.POST.get('date_range')).split(' - ')
        if len(date_range) > 1:
            date_from = parser.parse(date_range[0])
            date_to = parser.parse(date_range[1])
        else:
            date_from = parser.parse(date_range[0])
            date_to = date_from
        people = [person for person in people if person.date_added >= date_from and person.date_added <= date_to]

    questions_to_export = [field.question.display_name for field in CardDisplayedQuestion.objects.filter(person__user__pk=request.user.id)]

    format = request.GET.get('format', 'csv')
    if format in ['csv', 'json', 'html', 'xml']:
        return globals()["export_to_" + format](people, questions_to_export, request.user.id, _has_group(request.user, 'ADMIN'))

def people_filter(request):
    post_data = request.POST
    people_ids = post_data.getlist(u'people_ids[]')
    people = Person.objects.filter(pk__in=people_ids).order_by('id')
    filtered_people = view_helpers.filter_people(people, post_data)
    if post_data['order'] == 'relevance':
        filtered_people = sorted(filtered_people, key=lambda person: difflib.SequenceMatcher(
            None, post_data['filter_name'],
            person.get_full_name()).ratio(), reverse=True)
    response = render_to_string(template_name='profile/people_' + post_data['view'] + '.html',
        dictionary={'people': filtered_people}, context_instance=RequestContext(request))
    return HttpResponse(response, mimetype='application/html')

def people_sort(request):
    post_data = request.POST
    people_ids = post_data.getlist(u'people_ids[]')
    if post_data['inorder'] == 'ascending':
        inorder = False
    else:
        inorder = True
    if post_data['order'] in ['last_name', 'relevance']:
        people = Person.objects.filter(pk__in=people_ids)
        filtered_people = view_helpers.filter_people(people, post_data)
        filtered_people = sorted(filtered_people, key=lambda person: person.user.last_name.lower(), reverse=inorder)
        if post_data['order'] == 'relevance':
            filtered_people = sorted(filtered_people, key=lambda person: difflib.SequenceMatcher(
                None, post_data['filter_name'],
                person.user.last_name).ratio(), reverse=inorder)
    elif post_data['order'] == 'date_joined':
        if inorder:
            people = Person.objects.all().filter(pk__in=people_ids).order_by('user__' + post_data['order']).reverse()
        else:
            people = Person.objects.all().filter(pk__in=people_ids).order_by('user__' + post_data['order'])
        filtered_people = view_helpers.filter_people(people, post_data)
    elif post_data['order'] == 'location':
        people = Person.objects.filter(pk__in=people_ids)
        filtered_people = view_helpers.filter_people(people, post_data)
        filtered_people = sorted(filtered_people, key=lambda person: person.location_display_name.lower(), reverse=inorder)

    response = render_to_string(template_name='profile/people_' + post_data['view'] + '.html',
                                dictionary={'people': filtered_people}, context_instance=RequestContext(request))
    return HttpResponse(response, mimetype='application/html')

@login_required
@has_permissions(['can_view_people'])
@view
def people(request, order='date_joined'):
    """Display a list of people."""
    data = {}

    # pull in q from POST
    post_data = request.POST
    query = request.POST.get('q', '')

    # Store the raw query in the teemplate data
    data['raw_query'] = query

    # Parse the query, and store that in the template.
    parsed_query = mysite.profile.view_helpers.parse_string_query(query)
    data.update(parsed_query)

    # Get the list of people to display.
    if parsed_query['q'].strip():
        search_results = parsed_query['callable_searcher']()
        everybody, extra_data = search_results.people, search_results.template_data
        data.update(extra_data)
    else:
        everybody = Person.objects.filter(user__groups__name__iexact='VOLUNTEER').order_by('user__' + order)

    data['people'] = everybody

    # Add JS-friendly version of people data to template
    person_id_ranges = mysite.base.view_helpers.int_list2ranges([x.id for x in everybody])
    person_ids = ''
    for stop, start in person_id_ranges:
        if stop == start:
            person_ids += '%d,' % (stop,)
        else:
            person_ids += '%d-%d,' % (stop, start)

    people_ids = []
    for person in everybody:
        people_ids.append(int(person.id))

    questions = FormQuestion.objects.all()
    all_answers = FormAnswer.objects.filter(question__pk__in=[item.id for item in questions])
    answers = dict([
        ('Skills', []),
        ('HFOSS Organizations That Interest You', []),
        ('Causes That Interest You', []),
        ('Programming Languages, Frameworks, Environments', []),
        ('How much time would you like to commit to volunteering?', []),
        ('Have you previously contributed to open source projects?', [])
    ])
    for question in questions:
        answers[question.name] = []
        for answer in all_answers:
            if answer.question.id == question.id:
                answers[question.name].append(answer)

    data['people_ids'] = simplejson.dumps(people_ids)
    data['export_formats'] = {"csv": "CSV", "json": "JSON", "html": "HTML Table", 'xml': 'XML'}
    data['skills'] = answers['Skills']
    data['organizations'] = answers['HFOSS Organizations That Interest You']
    data['causes'] = answers['Causes That Interest You']
    data['languages'] = answers['Programming Languages, Frameworks, Environments']
    data['times_to_commit'] = answers['How much time would you like to commit to volunteering?']
    data['opensource'] = answers['Have you previously contributed to open source projects?']
    data['person_ids'] = simplejson.dumps(person_ids)
    data['order'] = order
    data['view'] = ('list' if request.user.get_profile().view_list else 'cards')
    return (request, 'profile/search_people.html', data)

def gimme_json_for_portfolio(request):
    "Get JSON used to live-update the portfolio editor."
    """JSON includes:
        * The person's data.
        * DataImportAttempts.
        * other stuff"""

    # Since this view is meant to be accessed asynchronously, it doesn't make
    # much sense to decorate it with @login_required, since this will redirect
    # the user to the login page. Not much use if the browser is requesting
    # this page async'ly! So let's use a different method that explicitly warns
    # the user if they're not logged in. At time of writing, this error message
    # is NOT displayed on screen. I suppose someone will see if it they're
    # using Firebug, or accessing the page synchronously.
    if not request.user.is_authenticated():
        return HttpResponseServerError("Oops, you're not logged in.")

    person = request.user.get_profile()

    # Citations don't naturally serialize summaries.
    citations = list(Citation.untrashed.filter(portfolio_entry__person=person))
    portfolio_entries_unserialized = PortfolioEntry.objects.filter(person=person, is_deleted=False)
    projects_unserialized = [p.project for p in portfolio_entries_unserialized]

    # Serialize citation summaries
    summaries = {}
    for c in citations:
        summaries[c.pk] = render_to_string(
                "profile/portfolio/citation_summary.html",
                {'citation': c})

    # FIXME: Maybe we can serialize directly to Python objects.
    # fixme: zomg       don't recycle variable names for objs of diff types srsly u guys!

    five_minutes_ago = datetime.datetime.utcnow() - datetime.timedelta(minutes=5)
    recent_dias = DataImportAttempt.objects.filter(person=person, date_created__gt=five_minutes_ago)
    recent_dias_json = simplejson.loads(serializers.serialize('json', recent_dias))
    portfolio_entries = simplejson.loads(serializers.serialize('json',
        portfolio_entries_unserialized))
    projects = simplejson.loads(serializers.serialize('json', projects_unserialized))
    # FIXME: Don't send like all the flippin projects down the tubes.
    citations = simplejson.loads(serializers.serialize('json', citations))

    recent_dias_that_are_completed = recent_dias.filter(completed=True)
    import_running = recent_dias.count() > 0 and (
            recent_dias_that_are_completed.count() != recent_dias.count())
    progress_percentage = 100
    if import_running:
        progress_percentage = int(recent_dias_that_are_completed.count() * 100.0 / recent_dias.count())
    import_data = {
            'running': import_running,
            'progress_percentage': progress_percentage,
            }

    json = simplejson.dumps({
        'dias': recent_dias_json,
        'import': import_data,
        'citations': citations,
        'portfolio_entries': portfolio_entries,
        'projects': projects,
        'summaries': summaries,
        'messages': request.user.get_and_delete_messages(),
        })

    return HttpResponse(json, mimetype='application/json')

def replace_icon_with_default(request):
    "Expected postcondition: project's icon_dict says it is generic."
    """
    Expected output will look something like this:
    {
            'success': true,
            'portfolio_entry__pk': 0
    }"""
    portfolio_entry = PortfolioEntry.objects.get(
            pk=int(request.POST['portfolio_entry__pk']),
            person__user=request.user)
    # FIXME: test for naughty people trying to replace others' icons with the default!
    project = portfolio_entry.project

    project_before_changes = mysite.search.models.Project.objects.get(pk=project.pk)

    # make a record of the old, wrong project icon in the database
    mysite.search.models.WrongIcon.spawn_from_project(project)

    try:
        wrong_icon_url = project_before_changes.icon_for_profile.url
    except ValueError:
        wrong_icon_url = "icon_url"

    # set project icon as default
    project.invalidate_all_icons()
    project.save()

    # email all@ letting them know that we did so
    from mysite.project.tasks import send_email_to_all_because_project_icon_was_marked_as_wrong
    send_email_to_all_because_project_icon_was_marked_as_wrong.delay(
            project__pk=project_before_changes.pk,
            project__name=project_before_changes.name,
            project_icon_url=wrong_icon_url)

    # prepare output
    data = {}
    data['success'] = True
    data['portfolio_entry__pk'] = portfolio_entry.pk
    return mysite.base.view_helpers.json_response(data)

@login_required
@csrf_exempt
def prepare_data_import_attempts_do(request):
    """
    Input: request.POST contains a list of usernames or email addresses.
    These are identifiers under which the authorized user has committed code
    to an open-source repository, or at least so says the user.

    Side-effects: Create DataImportAttempts that a user might want to execute.

    Not yet implemented: This means, don't show the user DIAs that relate to
    non-existent accounts on remote networks. And what *that* means is,
    before bothering the user, ask those networks beforehand if they even
    have accounts named identifiers[0], etc."""
    # {{{

    # For each commit identifier, prepare some DataImportAttempts.
    prepare_data_import_attempts(identifiers=request.POST.values(), user=request.user)

    return HttpResponse('1')
    # }}}

def prepare_data_import_attempts(identifiers, user):
    "Enqueue and track importation tasks."
    """Expected input: A list of committer identifiers, e.g.:
    ['paulproteus', 'asheesh@asheesh.org']

    For each data source, enqueue a background task.
    Keep track of information about the task in an object
    called a DataImportAttempt."""

    # Side-effects: Create DIAs that a user might want to execute.
    for identifier in identifiers:
        if identifier.strip(): # Skip blanks or whitespace
            for source_key, _ in DataImportAttempt.SOURCE_CHOICES:
                dia = DataImportAttempt(
                        query=identifier,
                        source=source_key,
                        person=user.get_profile())
                dia.save()
                dia.do_what_it_says_on_the_tin()

@has_permissions(['add_portfolioentry', 'change_portfolioentry'])
@login_required
@view
def importer(request, test_js = False):
    """Get the DIAs for the logged-in user's profile. Pass them to the template."""
    # {{{

    person = request.user.get_profile()
    data = get_personal_data(person)

    # This is used to create a blank 'Add another record' form, which is printed
    # to the bottom of the importer page. The HTML underlying this form is used
    # to generate forms dynamically.
    data['citation_form'] = mysite.profile.forms.ManuallyAddACitationForm(auto_id=False)

    # This variable is checked in base/templates/base/base.html
    data['test_js'] = test_js or request.GET.get('test', None)

    return (request, 'profile/importer.html', data)
    # }}}

#FIXME: Rename importer
portfolio_editor = importer

def portfolio_editor_test(request):
    return portfolio_editor(request, test_js=True)

def filter_by_key_prefix(dict, prefix):
    """Return those and only those items in a dictionary whose keys have the given prefix."""
    out_dict = {}
    for key, value in dict.items():
        if key.startswith(prefix):
            out_dict[key] = value
    return out_dict

@login_required
def user_selected_these_dia_checkboxes(request):
    """ Input: Request POST contains a list of checkbox IDs corresponding to DIAs.
    Side-effect: Make a note on the DIA that its affiliated person wants it.
    Output: Success?
    """
    # {{{

    prepare_data_import_attempts(request.POST, request.user)

    checkboxes = filter_by_key_prefix(request.POST, "person_wants_")
    identifiers = filter_by_key_prefix(request.POST, "identifier_")

    for checkbox_id, value in checkboxes.items():
        if value == 'on':
            x, y, identifier_index, source_key = checkbox_id.split('_')
            identifier = identifiers["identifier_%s" % identifier_index]
            if identifier:
                # FIXME: For security, ought this filter include only dias
                # associated with the logged-in user's profile?
                dia = DataImportAttempt(
                        identifier, source_key,
                        request.user.get_profile())
                dia.person_wants_data = True
                dia.save()
                dia.do_what_it_says_on_the_tin()

                # There may be data waiting or not,
                # but no matter; this function may
                # run unconditionally.
                dia.give_data_to_person()

    return HttpResponse('1')
    # }}}

@login_required
@view
def display_person_edit_name(request, name_edit_mode):
    '''Show a little edit form for first name and last name.

    Why separately handle first and last names? The Django user
    model already stores them separately.
    '''
    # {{{
    data = get_personal_data(request.user.get_profile())
    data['name_edit_mode'] = name_edit_mode
    data['editable'] = True
    return (request, 'profile/main.html', data)
    # }}}


@login_required
def display_person_edit_name_do(request):
    '''Take the new first name and last name out of the POST.

    Jam them into the Django user model.'''
    # {{{
    user = request.user

    new_first = request.POST['first_name']
    new_last = request.POST['last_name']

    user.first_name = new_first
    user.last_name = new_last
    user.save()

    return HttpResponseRedirect('/people/%s' % urllib.quote(user.username))
    # }}}

@login_required
def publish_citation_do(request):
    try:
        pk = request.POST['pk']
    except KeyError:
        return HttpResponse("0")

    try:
        c = Citation.objects.get(pk=pk, portfolio_entry__person__user=request.user)
    except Citation.DoesNotExist:
        return HttpResponse("0")

    c.is_published = True
    c.save()

    return HttpResponse("1")

@login_required
def delete_citation_do(request):
    try:
        pk = request.POST['citation__pk']
    except KeyError:
        return HttpResponse("0")

    try:
        c = Citation.objects.get(pk=pk, portfolio_entry__person__user=request.user)
    except Citation.DoesNotExist:
        return HttpResponse("0")

    c.is_deleted = True
    c.save()

    return HttpResponse("1")

@login_required
def delete_portfolio_entry_do(request):
    try:
        pk = int(request.POST['portfolio_entry__pk'])
    except KeyError:
        return mysite.base.view_helpers.json_response({'success': False})

    try:
        p = PortfolioEntry.objects.get(pk=pk, person__user=request.user)
    except PortfolioEntry.DoesNotExist:
        return mysite.base.view_helpers.json_response({'success': False})

    p.is_deleted = True
    p.save()

    return mysite.base.view_helpers.json_response({
            'success': True,
            'portfolio_entry__pk': pk})

@has_permissions(['add_portfolioentry', 'change_portfolioentry'])
@login_required
def save_portfolio_entry_do(request):
    pk = request.POST.get('portfolio_entry__pk', 'undefined')

    if pk == 'undefined':
        project, _ = Project.objects.get_or_create(name=request.POST['project_name'])
        p = PortfolioEntry(project=project, person=request.user.get_profile())
    else:
        p = PortfolioEntry.objects.get(pk=pk, person__user=request.user)
    p.project_description = request.POST['project_description']
    p.experience_description = request.POST['experience_description']
    p.receive_maintainer_updates = \
        request.POST['receive_maintainer_updates'].lower() not in ('false', '0')
    p.is_published = True
    p.save()

    # Publish all attached Citations
    citations = Citation.objects.filter(portfolio_entry=p)
    for c in citations:
        c.is_published = True
        c.save()

    return mysite.base.view_helpers.json_response({
            'success': True,
            'pf_entry_element_id': request.POST['pf_entry_element_id'],
            'project__pk': p.project_id,
            'portfolio_entry__pk': p.pk
        })

@login_required
def dollar_username(request):
    return HttpResponseRedirect(reverse(display_person_web,
		kwargs={'user_to_display__id':
                request.user.id}))

@login_required
def set_expand_next_steps_do(request):
    input_string = request.POST.get('value', None)
    string2value = {'True': True,
                    'False': False}
    if input_string not in string2value:
        return HttpResponseBadRequest("Bad POST.")

    person = request.user.get_profile()
    person.expand_next_steps = string2value[input_string]
    person.save()

    return HttpResponseRedirect(person.profile_url)

@login_required
@view
def edit_info(request, contact_blurb_error=False, edit_info_form=None, contact_blurb_form=None, has_errors=False, username=None):
    if username is not None and request.user.is_superuser:
        person = Person.objects.get(user__username__exact=username)
    else:
        person = request.user.get_profile()
    data = get_personal_data(person)
    data['info_edit_mode'] = True
    if edit_info_form is None:
        edit_info_form = mysite.profile.forms.EditInfoForm(prefix='edit-tags', person=person, initial={
            'first_name': person.user.first_name,
            'last_name': person.user.last_name,
            'email': person.user.email
        })

    if contact_blurb_form is None:
        contact_blurb_form = mysite.profile.forms.ContactBlurbForm(initial={
          'contact_blurb': person.contact_blurb,
        }, prefix='edit-tags')
    data['form'] = edit_info_form
    data['contact_blurb_form'] = contact_blurb_form
    data['contact_blurb_error'] = contact_blurb_error
    data['forwarder_sample'] = mysite.base.view_helpers.put_forwarder_in_contact_blurb_if_they_want("$fwd", person.user)
    data['has_errors'] = has_errors
    data['person_id'] = person.id
    return request, 'profile/info_wrapper.html', data

@login_required
def set_pfentries_dot_use_my_description_do(request):
    project = Project.objects.get(pk=request.POST['project_pk'])
    pfe_pks = project.portfolioentry_set.values_list('pk', flat=True)
    Form = mysite.profile.forms.UseDescriptionFromThisPortfolioEntryForm
    for pfe_pk in pfe_pks:
        pfe_before_save = PortfolioEntry.objects.get(pk=pfe_pk)
        form = Form(request.POST,
                instance=pfe_before_save,
                prefix=str(pfe_pk))
        if form.is_valid():
            pfe_after_save = form.save()
            logging.info("Project description settings edit: %s just edited a project.  The portfolioentry's data originally read as follows: %s.  Its data now read as follows: %s" % (
                request.user.get_profile(), pfe_before_save.__dict__, pfe_after_save.__dict__))
    return HttpResponseRedirect(project.get_url())

@view
def unsubscribe(request, token_string):
    context = {'unsubscribe_this_user':
            mysite.profile.models.UnsubscribeToken.whose_token_string_is_this(token_string),
            'token_string': token_string}
    return (request, 'unsubscribe.html', context)

def unsubscribe_do(request):
    token_string = request.POST.get('token_string', None)
    person = mysite.profile.models.UnsubscribeToken.whose_token_string_is_this(token_string)
    person.email_me_re_projects = False
    person.save()
    return HttpResponseRedirect(reverse(unsubscribe, kwargs={'token_string': token_string}))

@login_required
def bug_recommendation_list_as_template_fragment(request):
    suggested_searches = request.user.get_profile().get_recommended_search_terms()
    recommender = mysite.profile.view_helpers.RecommendBugs(
        suggested_searches, n=5)
    recommended_bugs = list(recommender.recommend())

    response_data = {}

    if recommended_bugs:
        response_data['result'] = 'OK'
        template_path = 'base/recommended_bugs_content.html'
        context = RequestContext(request, { 'recommended_bugs': recommended_bugs })
        response_data['html'] = render_to_string(template_path, context)
    else:
        response_data['result'] = 'NO_BUGS'

    return HttpResponse(simplejson.dumps(response_data), mimetype='application/json')

### API-y views go below here
class LocationDataApiView(django.views.generic.View):
    ### Entry point for requests from the web
    def get(self, request):
        person_ids = self.extract_person_ids(request.GET)
        data_dict = self.raw_data_for_person_ids(person_ids)
        as_json = simplejson.dumps(data_dict)
        return HttpResponse(as_json, mimetype='application/javascript')

    ### Helper functions
    @staticmethod
    def raw_data_for_person_ids(person_ids):
        persons = mysite.profile.models.Person.objects.filter(
            id__in=person_ids).select_related()
        return LocationDataApiView.raw_data_for_person_collection(persons)

    @staticmethod
    def raw_data_for_person_collection(people):
        person_id2data = dict([
                (person.pk, LocationDataApiView.raw_data_for_one_person(person))
                for person in people])
        return person_id2data

    @staticmethod
    def raw_data_for_one_person(person):
        location = person.get_public_location_or_default()
        name = person.get_full_name_or_username()
        ret = {
            'name': name,
            'location': location,
            }
        ret['lat_long_data'] = {
            'is_inaccessible': (location == mysite.profile.models.DEFAULT_LOCATION),
            'latitude': person.get_public_latitude_or_default(),
            'longitude': person.get_public_longitude_or_default(),
            }
        extra_person_info = {'username': person.user.username,
                             'photo_thumbnail_url': person.get_photo_url_or_default(),
                             }
        ret['extra_person_info'] = extra_person_info
        return ret

    @staticmethod
    def range_from_string(s):
        on_hyphens = s.split('-')
        if len(on_hyphens) != 2:
            return None

        try:
            from_, to = map(int, on_hyphens)
        except ValueError:
            return None

        return range(from_, to + 1)

    @staticmethod
    def extract_person_ids(get_data):
        person_ids_as_string = get_data.get('person_ids', '')
        id_set = set()
        if not person_ids_as_string:
            return id_set

        splitted_from_commas = person_ids_as_string.split(',')
        for item in splitted_from_commas:
            if '-' in item:
                as_ints = LocationDataApiView.range_from_string(item)
                if as_ints is not None:
                    id_set.update(as_ints)
                continue

            try:
                as_int = int(item)
            except ValueError:
                continue
            id_set.add(as_int)
        return id_set


# vim: ai ts=3 sts=4 et sw=4 nu
