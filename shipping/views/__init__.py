# ***** BEGIN LICENSE BLOCK *****
# Version: MPL 1.1/GPL 2.0/LGPL 2.1
#
# The contents of this file are subject to the Mozilla Public License Version
# 1.1 (the "License"); you may not use this file except in compliance with
# the License. You may obtain a copy of the License at
# http://www.mozilla.org/MPL/
#
# Software distributed under the License is distributed on an "AS IS" basis,
# WITHOUT WARRANTY OF ANY KIND, either express or implied. See the License
# for the specific language governing rights and limitations under the
# License.
#
# The Original Code is l10n django site.
#
# The Initial Developer of the Original Code is
# Mozilla Foundation.
# Portions created by the Initial Developer are Copyright (C) 2010
# the Initial Developer. All Rights Reserved.
#
# Contributor(s):
#
# Alternatively, the contents of this file may be used under the terms of
# either the GNU General Public License Version 2 or later (the "GPL"), or
# the GNU Lesser General Public License Version 2.1 or later (the "LGPL"),
# in which case the provisions of the GPL or the LGPL are applicable instead
# of those above. If you wish to allow use of your version of this file only
# under the terms of either the GPL or the LGPL, and not to allow others to
# use your version of this file under the terms of the MPL, indicate your
# decision by deleting the provisions above and replace them with the notice
# and other provisions required by the GPL or the LGPL. If you do not delete
# the provisions above, a recipient may use your version of this file under
# the terms of any one of the MPL, the GPL or the LGPL.
#
# ***** END LICENSE BLOCK *****

'''Views for managing sign-offs and shipping metrics.
'''

from django.shortcuts import get_object_or_404, render_to_response
from django.template import RequestContext
from django.template.loader import render_to_string
from django.http import HttpResponseRedirect, HttpResponse
from life.models import Repository, Locale, Push, Changeset, Tree
from shipping.models import Milestone, Signoff, Snapshot, AppVersion, Action, SignoffForm, ActionForm
from l10nstats.models import Run, Run_Revisions
from todo.views import snippets, new as create_new_wizard
from todo.models import Task, Tracker
from django import forms
from django.conf import settings
from django.core.urlresolvers import reverse
from django.views.decorators.cache import cache_control
from django.utils import simplejson
from django.utils.http import urlencode
from django.utils.safestring import mark_safe
from django.core import serializers
from django.db import connection
from django.db.models import Max

from collections import defaultdict
from ConfigParser import ConfigParser
import datetime
from difflib import SequenceMatcher
import re
import urllib

from Mozilla.Parser import getParser, Junk
from Mozilla.CompareLocales import AddRemove, Tree as DataTree


def index(request):
    locales = Locale.objects.all().order_by('code')
    avs = AppVersion.objects.all().order_by('code')

    for i in avs:
        statuses = Milestone.objects.filter(appver=i.id).values_list('status', flat=True).distinct()
        if 1 in statuses:
            i.status = 'open'
        elif 0 in statuses:
            i.status = 'upcoming'
        elif 2 in statuses:
            i.status = 'shipped'
        else:
            i.status = 'unknown' 

    return render_to_response('shipping/index.html', {
        'locales': locales,
        'avs': avs,
    })

def homesnippet(request):
    q = AppVersion.objects.filter(milestone__status=1).select_related('app')
    q = q.order_by('app__name','-version')
    return render_to_string('shipping/snippet.html', {
            'appvers': q,
            })

def teamsnippet(request, locale):
    active_runs = locale.run_set.filter(active__isnull=False)
    trees_ids = list(active_runs.values_list('tree', flat=True))
    q = AppVersion.objects.filter(tree__in=trees_ids)
    q = q.order_by('app__name','-version')
    # appvers is a list of tuples and not a dict to preserve the ordering of 
    # the queryset
    appvers = []
    for av in q:
        appvers.append((av, av.todo.task_count(locale)))

    return render_to_string('shipping/team-snippet.html', {
            'locale': locale,
            'appvers': appvers,
            })

def task(request, task_id):
    task = get_object_or_404(Task, pk=task_id)
    task_snippet = snippets.task(request, task,
                         redirect_view='shipping.views.task')
    return render_to_response('shipping/task.html',
                              {'task': task,
                               'task_snippet': task_snippet,
                               # these are needed to make the log-in form 
                               # reload the page
                               'request': request,
                               'login_form_needs_reload': True,})

def tracker(request, tracker_id):
    tracker = get_object_or_404(Tracker, pk=tracker_id)
    tree = snippets.tree(request, tracker=tracker,
                         task_view='shipping.views.task',
                         tracker_view='shipping.views.tracker')
    return render_to_response('shipping/tree.html',
                              {'tracker': tracker,
                               'tree': tree,
                               # these are needed to make the log-in form 
                               # reload the page
                               'request': request,
                               'login_form_needs_reload': True,})

def new_todo(request):
    def locale_filter(appver):
        active_runs = appver.tree.run_set.filter(active__isnull=False)
        locales_ids = list(active_runs.values_list('locale', flat=True))
        return Locale.objects.filter(id__in=locales_ids)
    appvers = AppVersion.objects.filter(milestone__status=1)
    appvers = appvers.select_related('app').order_by('app__name','-version')
    config = {
        'projects': appvers,
        'locale_filter': locale_filter,
        #'get_template': lambda step: 'todo/new_%d.html' % step,
        'task_view': 'shipping.views.task',
        'tracker_view': 'shipping.views.tracker',
        'thankyou_view': 'todo.views.created',
    }
    return create_new_wizard(request, **config)

def pushes(request):
    if request.GET.has_key('locale'):
        locale = Locale.objects.get(code=request.GET['locale'])
    if request.GET.has_key('ms'):
        mstone = Milestone.objects.get(code=request.GET['ms'])
        appver = mstone.appver
    if request.GET.has_key('av'):
        appver = AppVersion.objects.get(code=request.GET['av'])
        try:
            mstone = Milestone.objects.filter(appver__code=request.GET['av']).order_by('-pk')[0]
        except:
            mstone = None
    enabled = mstone is not None and mstone.status==1
    if enabled:
        current = _get_current_signoff(locale, ms=mstone, av=appver)
    else:
        current = _get_accepted_signoff(locale, ms=mstone, av=appver)
    user = request.user
    anonymous = user.is_anonymous()
    staff = 'drivers' in user.groups.values_list('name', flat=True)
    if request.method == 'POST': # we're going to process forms
        offset_id = request.POST['first_row']
        if not enabled: # ... but we're not logged in. Panic!
            request.session['signoff_error'] = u'<span style="font-style: italic">Signoff for %s %s</span> could not be added - <strong>Milestone is not open for edits</strong>' % (mstone, locale)
        elif anonymous: # ... but we're not logged in. Panic!
            request.session['signoff_error'] = u'<span style="font-style: italic">Signoff for %s %s</span> could not be added - <strong>User not logged in</strong>' % (appver, locale)
        else:
            if request.POST.has_key('accepted'): # we're in AcceptedForm mode
                if not staff: # ... but we have no privileges for that!
                    request.session['signoff_error'] = u'<span style="font-style: italic">Signoff for %s %s</span> could not be accepted/rejected - <strong>User has not enough privileges</strong>' % (mstone or appver, locale)
                else:
                    # hack around AcceptForm not taking strings, fixed in
                    # django 1.1
                    bval = {"true": 1, "false": 2}[request.POST['accepted']]
                    form = ActionForm({'signoff': current.id, 'flag': bval, 'author': user.id, 'comment': request.POST['comment']})
                    if form.is_valid():
                        form.save()
                        if request.POST['accepted'] == "false":
                            request.session['signoff_info'] = '<span style="font-style: italic">Rejected'
                        else:
                            request.session['signoff_info'] = '<span style="font-style: italic">Accepted'
                    else:
                        request.session['signoff_error'] = u'<span style="font-style: italic">Signoff for %s %s by %s</span> could not be added' % (mstone or appver, locale, user.username)
            else:
                instance = Signoff(appversion=appver, locale=locale, author=user)
                form = SignoffForm(request.POST, instance=instance)
                if form.is_valid():
                    form.save()
                    
                    #add a snapshot of the current test results
                    pushobj = Push.objects.get(id=request.POST['push'])
                    lastrun = _get_compare_locales_result(pushobj.tip, appver.tree)
                    if lastrun:
                        Snapshot.objects.create(signoff_id=form.instance.id, test=Run, tid=lastrun.id)
                    Action.objects.create(signoff_id=form.instance.id, flag=0, author=user)

                    request.session['signoff_info'] = u'<span style="font-style: italic">Signoff for %s %s by %s</span> added' % (mstone or appver, locale, user.username)
                else:
                    request.session['signoff_error'] = u'<span style="font-style: italic">Signoff for %s %s by %s</span> could not be added' % (mstone or appver, locale, user.username)
        if request.GET.has_key('av'):
            return HttpResponseRedirect('%s?locale=%s&av=%s&offset=%s' % (reverse('shipping.views.pushes'), locale.code ,appver.code, offset_id))
        else:
            return HttpResponseRedirect('%s?locale=%s&ms=%s&offset=%s' % (reverse('shipping.views.pushes'), locale.code ,mstone.code, offset_id))

    form = SignoffForm()
    
    forest = appver.tree.l10n
    repo_url = '%s%s/' % (forest.url, locale.code)
    notes = _get_notes(request.session)
    accepted = _get_accepted_signoff(locale, ms=mstone, av=appver)
    if accepted is None:
        # no accepted signoff to diff against, let's try the latest 
        # obsolete one
        accepted = _signoffs(mstone is None and appver or mstone, status=4,
                             locale=locale.code)

    branches = re.split(r', *', request.GET['branches']) if request.GET.has_key('branches') else None

    max_pushes = _get_total_pushes(locale, mstone, branches)
    if max_pushes > 50:
        max_pushes = 50

    if request.GET.has_key('center'):
        offset = _get_push_offset(repo_url, request.GET['center'], -5, branches=branches)
    elif request.GET.has_key('offset'):
        offset = _get_push_offset(repo_url, request.GET['offset'], branches=branches)
    else:
        offset = 0

    return render_to_response('shipping/pushes.html', {
        'mstone': mstone,
        'appver': appver,
        'locale': locale,
        'form': form,
        'notes': notes,
        'current': current,
        'accepted': accepted,
        'user': user,
        'user_type': 0 if anonymous else 2 if staff else 1,
        'pushes': (simplejson.dumps(_get_api_items(locale, appver, current, offset=offset+20, branches=branches)), 0, min(max_pushes,offset+10)),
        'max_pushes': max_pushes,
        'offset': offset,
        'branches': request.GET.get('branches', None),
        'current_js': simplejson.dumps(_get_signoff_js(current)),
        'login_form_needs_reload': True,
        'request': request,
    })


def __universal_le(content):
    "CompareLocales reads files with universal line endings, fake that"
    return content.replace('\r\n','\n').replace('\r','\n')

def diff_app(request):
    # XXX TODO: error handling
    reponame = request.GET['repo']
    repopath = settings.REPOSITORY_BASE + '/' + reponame
    repo_url = Repository.objects.filter(name=reponame).values_list('url', flat=True)[0]
    from mercurial.ui import ui as _ui
    from mercurial.hg import repository
    ui = _ui()
    repo = repository(ui, repopath)
    ctx1 = repo.changectx(request.GET['from'])
    ctx2 = repo.changectx(request.GET['to'])
    match = None # maybe get something from l10n.ini and cmdutil
    changed, added, removed = repo.status(ctx1, ctx2, match=match)[:3]
    diffs = DataTree(dict)
    for path in added:
        diffs[path].update({'path': path,
                            'isFile': True,
                            'rev': request.GET['to'],
                            'desc': ' (File added)',
                            'class': 'added'})
    for path in removed:
        diffs[path].update({'path': path,
                            'isFile': True,
                            'rev': request.GET['from'],
                            'desc': ' (File removed)',
                            'class': 'removed'})
    for path in changed:
        lines = []
        try:
            p = getParser(path)
        except UserWarning:
            diffs[path].update({'path': path,
                                'lines': [{'class': 'issue',
                                           'oldval': '',
                                           'newval': '',
                                           'entity': 'cannot parse ' + path}]})
            continue
        data1 = ctx1.filectx(path).data()
        data2 = ctx2.filectx(path).data()
        try:
            # parsing errors or such can break this, catch those and fail
            # gracefully
            # fake reading with universal line endings, too
            p.readContents(__universal_le(data1))
            a_entities, a_map = p.parse()
            p.readContents(__universal_le(data2))
            c_entities, c_map = p.parse()
            del p
        except:
            diffs[path].update({'path': path,
                                'lines': [{'class': 'issue',
                                           'oldval': '',
                                           'newval': '',
                                           'entity': 'cannot parse ' + path}]})
            continue            
        a_list = sorted(a_map.keys())
        c_list = sorted(c_map.keys())
        ar = AddRemove()
        ar.set_left(a_list)
        ar.set_right(c_list)
        for action, item_or_pair in ar:
            if action == 'delete':
                lines.append({'class': 'removed',
                              'oldval': [{'value':a_entities[a_map[item_or_pair]].val}],
                              'newval': '',
                              'entity': item_or_pair})
            elif action == 'add':
                lines.append({'class': 'added',
                              'oldval': '',
                              'newval':[{'value': c_entities[c_map[item_or_pair]].val}],
                              'entity': item_or_pair})
            else:
                oldval = a_entities[a_map[item_or_pair[0]]].val
                newval = c_entities[c_map[item_or_pair[1]]].val
                if oldval == newval:
                    continue
                sm = SequenceMatcher(None, oldval, newval)
                oldhtml = []
                newhtml = []
                for op, o1, o2, n1, n2 in sm.get_opcodes():
                    if o1 != o2:
                        oldhtml.append({'class':op, 'value':oldval[o1:o2]})
                    if n1 != n2:
                        newhtml.append({'class':op, 'value':newval[n1:n2]})
                lines.append({'class':'changed',
                              'oldval': oldhtml,
                              'newval': newhtml,
                              'entity': item_or_pair[0]})
        container_class = lines and 'file' or 'empty-diff'
        diffs[path].update({'path': path,
                            'class': container_class,
                            'lines': lines})
    diffs = diffs.toJSON().get('children', [])
    return render_to_response('shipping/diff.html',
                              {'given_title': request.GET.get('title', None),
                               'repo': reponame,
                               'repo_url': repo_url,
                               'old_rev': request.GET['from'],
                               'new_rev': request.GET['to'],
                               'diffs': diffs})


def dashboard(request):
    args = [] # params to pass to l10nstats json
    query = [] # params to pass to shipping json
    subtitles = []
    if 'ms' in request.GET:
        mstone = Milestone.objects.get(code=request.GET['ms'])
        args.append(('tree', mstone.appver.tree.code))
        subtitles.append(str(mstone))
        query.append(('ms', mstone.code))
    elif 'av' in request.GET:
        appver = AppVersion.objects.get(code=request.GET['av'])
        args.append(('tree', appver.tree.code))
        subtitles.append(str(appver))
        query.append(('av', appver.code))

    # sanitize the list of locales to those that are actually on the dashboard
    locales = Locale.objects.filter(code__in=request.GET.getlist('locale'))
    locales = locales.values_list('code', flat=True)
    args += [("locale", loc) for loc in locales]
    query += [("locale", loc) for loc in locales]
    subtitles += list(locales)

    return render_to_response('shipping/dashboard.html', {
            'subtitles': subtitles,
            'query': mark_safe(urlencode(query)),
            'args': mark_safe(urlencode(args)),
            })

@cache_control(max_age=60)
def l10n_changesets(request):
    if request.GET.has_key('ms'):
        av_or_m = Milestone.objects.get(code=request.GET['ms'])
    elif request.GET.has_key('av'):
        av_or_m = AppVersion.objects.get(code=request.GET['av'])
    else:
        return HttpResponse('No milestone or appversion given')

    sos = _signoffs(av_or_m).annotate(tip=Max('push__changesets__id'))
    tips = dict(sos.values_list('locale__code', 'tip'))
    revmap = dict(Changeset.objects.filter(id__in=tips.values()).values_list('id', 'revision'))
    r = HttpResponse(('%s %s\n' % (l, revmap[tips[l]][:12])
                      for l in sorted(tips.keys())),
                     content_type='text/plain; charset=utf-8')
    r['Content-Disposition'] = 'inline; filename=l10n-changesets'
    return r

@cache_control(max_age=60)
def shipped_locales(request):
    if request.GET.has_key('ms'):
        av_or_m = Milestone.objects.get(code=request.GET['ms'])
    elif request.GET.has_key('av'):
        av_or_m = AppVersion.objects.get(code=request.GET['av'])
    else:
        return HttpResponse('No milestone or appversion given')

    sos = _signoffs(av_or_m).values_list('locale__code', flat=True)
    locales = list(sos) + ['en-US']
    def withPlatforms(loc):
        if loc == 'ja':
            return 'ja linux win32\n'
        if loc == 'ja-JP-mac':
            return 'ja-JP-mac osx\n'
        return loc + '\n'
    
    r = HttpResponse(map(withPlatforms, sorted(locales)),
                      content_type='text/plain; charset=utf-8')
    r['Content-Disposition'] = 'inline; filename=shipped-locales'
    return r

@cache_control(max_age=60)
def signoff_json(request):
    appvers = AppVersion.objects
    if request.GET.has_key('ms'):
        av_or_m = Milestone.objects.get(code=request.GET['ms'])
        appvers = appvers.filter(app=av_or_m.appver.app)
    elif request.GET.has_key('av'):
        av_or_m = AppVersion.objects.get(code=request.GET['av'])
        appvers = appvers.filter(app=av_or_m.app)
    else:
        av_or_m = None
        appvers = appvers.all()
    tree2av = dict(AppVersion.objects.values_list("tree__code","code"))
    locale = request.GET.get('locale', None)
    lsd = _signoffs(av_or_m, getlist=True, locale=locale)
    items = defaultdict(list)
    values = dict(Action._meta.get_field('flag').flatchoices)
    for k, sol in lsd.iteritems():
        items[k] = [values[so] for so in sol]
    # get shipped-in data, latest milestone of all appversions for now
    shipped_in = defaultdict(list)
    for _av in appvers:
        try:
            _ms = _av.milestone_set.filter(status=2).order_by('-pk')[0]
        except IndexError:
            continue
        tree = _ms.appver.tree.code
        _sos = _ms.signoffs
        if locale is not None:
            _sos = _sos.filter(locale__code=locale)
        for loc in _sos.values_list('locale__code', flat=True):
            shipped_in[(tree, loc)].append(_ms.code)
    # make a list now
    items = [{"type": "SignOff",
              "label": "%s/%s" % (tree,locale),
              "tree": tree,
              "signoff": list(values)}
             for (tree, locale), values in items.iteritems()]
    items += [{"type": "Shippings",
               "label": "%s/%s" % (tree,locale),
               "shipped": stones}
              for (tree, locale), stones in shipped_in.iteritems()]
    items += [{"type": "AppVer4Tree",
               "label": tree,
               "appversion": av}
              for tree, av in tree2av.iteritems()]
    return HttpResponse(simplejson.dumps({'items': items}, indent=2),
                        mimetype="text/plain")


@cache_control(max_age=60)
def pushes_json(request):
    loc = request.GET.get('locale', None)
    ms = request.GET.get('mstone', None)
    appver = request.GET.get('av', None)
    start = int(request.GET.get('from', 0))
    to = int(request.GET.get('to', 20))
    branches = re.split(r', *', request.GET['branches']) if request.GET.has_key('branches') else None
    
    locale = None
    mstone = None
    cur = None
    if loc:
        locale = Locale.objects.get(code=loc)
    if ms:
        mstone = Milestone.objects.get(code=ms)
        appver = mstone.appver
    elif appver:
        appver = AppVersion.objects.get(code=appver)
    if loc and ms:
        cur = _get_current_signoff(locale, mstone)
    
    pushes = _get_api_items(locale, appver, cur, start=start, offset=start+to, branches=branches)
    return HttpResponse(simplejson.dumps({'items': pushes}, indent=2))


def milestones(request):
    """Administrate milestones.

    Opens an exhibit that offers the actions below depending on 
    milestone status and user permissions.
    """
    # we need to use {% url %} with an exhibit {{.foo}} as param,
    # fake { and } to be safe in urllib.quote, which is what reverse
    # calls down the line.
    if '{' not in urllib.always_safe:
        always_safe = urllib.always_safe
        urllib.always_safe = always_safe + '{}'
    else:
        always_safe = None
    r =  render_to_response('shipping/milestones.html',
                            {'login_form_needs_reload': True,
                             'request': request,
                             },
                            context_instance=RequestContext(request))
    if always_safe is not None:
        urllib.always_safe = always_safe
    return r

@cache_control(max_age=60)
def stones_data(request):
    """JSON data to be used by milestones
    """
    latest = defaultdict(int)
    items = []
    stones = Milestone.objects.order_by('-pk').select_related('appver__app')
    maxage = 5
    for stone in stones:
        age = latest[stone.appver.id]
        if age >= maxage:
            continue
        latest[stone.appver.id] += 1
        items.append({'label': str(stone),
                      'appver': str(stone.appver),
                      'status': stone.status,
                      'code': stone.code,
                      'age': age})

    return HttpResponse(simplejson.dumps({'items': items}, indent=2))

def open_mstone(request):
    """Open a milestone.

    Only available to POST, and requires signoff.can_open permissions.
    Redirects to milestones().
    """
    if (request.method == "POST" and
        'ms' in request.POST and
        request.user.has_perm('shipping.can_open')):
        try:
            mstone = Milestone.objects.get(code=request.POST['ms'])
            mstone.status = 1
            # XXX create event
            mstone.save()
        except:
            pass
    return HttpResponseRedirect(reverse('shipping.views.milestones'))

def clear_mstone(request):
    """Clear a milestone, reset all sign-offs.

    Only available to POST, and requires signoff.can_open permissions.
    Redirects to dasboard() for the milestone.
    """
    if (request.method == "POST" and
        'ms' in request.POST and
        request.user.has_perm('shipping.can_open')):
        try:
            mstone = Milestone.objects.get(code=request.POST['ms'])
            if mstone.status is 2:
                return HttpResponseRedirect(reverse('shipping.views.milestones'))
            # get all signoffs, independent of state, and file an obsolete
            # action
            for so in _signoffs(mstone, status=None):
                so.action_set.create(flag=4, author=request.user)
            return HttpResponseRedirect(reverse('shipping.views.dashboard')
                                        + "?ms=" + mstone.code)
        except:
            pass
    return HttpResponseRedirect(reverse('shipping.views.milestones'))


def _propose_mstone(mstone):
    """Propose a new milestone based on an existing one.

    Tries to find the last integer in name and version, increment that
    and create a new milestone.
    """
    last_int = re.compile('(\d+)$')
    name_m = last_int.search(mstone.name)
    if name_m is None:
        return None
    code_m = last_int.search(mstone.code)
    if code_m is None:
        return None
    name_int = int(name_m.group())
    code_int = int(code_m.group())
    if name_int != code_int:
        return None
    new_rev = str(name_int + 1)
    return dict(code=last_int.sub(new_rev, mstone.code),
                name=last_int.sub(new_rev, mstone.name),
                appver=mstone.appver.code)


def confirm_ship_mstone(request):
    """Intermediate page when shipping a milestone.

    Gathers all data to verify when shipping.
    Ends up in ship_mstone if everything is fine.
    Redirects to milestones() in case of trouble.
    """
    if not ("ms" in request.GET):
        return HttpResponseRedirect(reverse('shipping.views.milestones'))
    try:
        mstone = Milestone.objects.get(code=request.GET['ms'])
    except:
        return HttpResponseRedirect(reverse('shipping.views.milestones'))
    if mstone.status != 1:
        return HttpResponseRedirect(reverse('shipping.views.milestones'))
    statuses = _signoffs(mstone, getlist=True)
    pending_locs = []
    good = 0
    for (tree, loc), flags in statuses.iteritems():
        if 0 in flags:
            # pending
            pending_locs.append(loc)
        if 1 in flags:
            # good
            good += 1
    pending_locs.sort()
    return render_to_response('shipping/confirm-ship.html',
                              {'mstone': mstone,
                               'pending_locs': pending_locs,
                               'good': good,
                               'login_form_needs_reload': True,
                               'request': request,
                             },
                              context_instance=RequestContext(request))
        
def ship_mstone(request):
    """The actual worker method to ship a milestone.

    Only avaible to POST.
    Redirects to milestones().
    """
    if (request.method == "POST" and
        'ms' in request.POST and
        request.user.has_perm('shipping.can_ship')):
        try:
            mstone = Milestone.objects.get(code=request.POST['ms'])
            # get current signoffs
            cs = _signoffs(mstone).values_list('id', flat=True)
            mstone.signoffs.add(*list(cs))  # add them
            mstone.status = 2
            # XXX create event
            mstone.save()
        except:
            pass
    return HttpResponseRedirect(reverse('shipping.views.milestones'))


def confirm_drill_mstone(request):
    """Intermediate page when fire-drilling a milestone.

    Gathers all data to verify when shipping.
    Ends up in drill_mstone if everything is fine.
    Redirects to milestones() in case of trouble.
    """
    if not ("ms" in request.GET and
            request.user.has_perm('shipping.can_ship')):
        return HttpResponseRedirect(reverse('shipping.views.milestones'))
    try:
        mstone = Milestone.objects.get(code=request.GET['ms'])
    except:
        return HttpResponseRedirect(reverse('shipping.views.milestones'))
    if mstone.status != 1:
        return HttpResponseRedirect(reverse('shipping.views.milestones'))

    drill_base = Milestone.objects.filter(appver=mstone.appver,status=2).order_by('-pk').select_related()
    proposed = _propose_mstone(mstone)

    return render_to_response('shipping/confirm-drill.html',
                              {'mstone': mstone,
                               'older': drill_base[:3],
                               'proposed': proposed,
                               'login_form_needs_reload': True,
                               'request': request,
                               },
                              context_instance=RequestContext(request))

def drill_mstone(request):
    """The actual worker method to ship a milestone.

    Only avaible to POST.
    Redirects to milestones().
    """
    if (request.method == "POST" and
        'ms' in request.POST and
        'base' in request.POST and
        request.user.has_perm('shipping.can_ship')):
        try:
            mstone = Milestone.objects.get(code=request.POST['ms'])
            base = Milestone.objects.get(code=request.POST['base'])
            so_ids = list(base.signoffs.values_list('id', flat=True))
            mstone.signoffs = so_ids  # add signoffs of base ms
            mstone.status = 2
            # XXX create event
            mstone.save()
        except Exception, e:
            pass
    return HttpResponseRedirect(reverse('shipping.views.milestones'))


#
#  Internal functions
#

def _get_pushes(branches=None):
    if branches is None:
        return Push.objects.filter(changesets__branch__id=1).distinct()
    elif 'all' in branches:
        return Push.objects
    else:
        return Push.objects.filter(changesets__branch__name__in=branches).distinct()

def _get_current_signoff(locale, ms=None, av=None):
    if av:
        sos = Signoff.objects.filter(locale=locale, appversion=av)
    else:
        sos = Signoff.objects.filter(locale=locale, appversion=ms.appver)
    try:
        return sos.order_by('-pk')[0]
    except IndexError:
        return None

def _get_total_pushes(locale=None, mstone=None, branches=None):
    pushobjs = _get_pushes(branches)
    if mstone:
        forest = mstone.appver.tree.l10n
        repo_url = '%s%s/' % (forest.url, locale.code) 
        return pushobjs.filter(repository__url=repo_url).count()
    else:
        return pushobjs.count()

def _get_compare_locales_result(rev, tree):
        try:
            return Run.objects.filter(revisions=rev,
                                      tree=tree).order_by('-build__id')[0]
        except:
            return None

def _get_api_items(locale, appver=None, current=None, start=0, offset=10, branches=None):
    pushobjs = _get_pushes(branches)
    if appver:
        forest = appver.tree.l10n
        repo_url = '%s%s/' % (forest.url, locale.code) 
        pushobjs = pushobjs.filter(repository__url=repo_url).order_by('-push_date')[start:start+offset]
    else:
        pushobjs = pushobjs.order_by('-push_date')[start:start+offset]
    
    pushes = []
    if current:
        current_push = current.push.id
        current_accepted = current.accepted
    else:
        current_push = None
        current_accepted = None
    tipmap = dict(pushobjs.annotate(tip_id=Max('changesets')).values_list('id','tip_id'))
    revmap = dict((id, (rev[:12], desc)) for id, rev, desc in Changeset.objects.filter(id__in=tipmap.values()).values_list('id','revision', 'description'))
    rq = Run_Revisions.objects.filter(changeset__in=tipmap.values())
    if appver:
        rq = rq.filter(run__tree=appver.tree)
    rq = rq.order_by('run__build__id')
    runs = {}
    for d in rq.values('run__build__id','changeset', 'run__tree','run__missing','run__missingInFiles', 'run__errors','run__obsolete', 'run__completion'):
        runs[(d['run__tree'],d['changeset'])] = d
    for pushobj in pushobjs.select_related('repository'):
        if appver:
            signoff_trees = [appver.tree]
        else:
            signoff_trees = Tree.objects.filter(l10n__repositories=pushobj.repository, appversion__milestone__isnull=False)
        name = '%s on [%s]' % (pushobj.user, pushobj.push_date)
        date = pushobj.push_date.strftime("%Y-%m-%d")
        cur = current_push and current_push == pushobj.id

        # check compare-locales
        for tree in signoff_trees:
            try:
                lastrun = runs[(tree.id,tipmap[pushobj.id])]
                missing = lastrun['run__missing'] + lastrun['run__missingInFiles']
                cmp_segs = []
                if lastrun['run__errors']:
                    cmp_segs.append('%d error(s)' % lastrun['run__errors'])
                if missing:
                    cmp_segs.append('%d missing' % missing)
                if lastrun['run__obsolete']:
                    cmp_segs.append('%d obsolete' % lastrun['run__obsolete'])
                if cmp_segs:
                    compare = ', '.join(cmp_segs)
                else:
                    compare = 'green (%d%%)' % lastrun['run__completion']
            except Exception, e:
                compare = 'no build'

            tiprev, tipdesc = revmap[tipmap[pushobj.id]]
            pushes.append({'name': name,
                           'date': date,
                           'time': pushobj.push_date.strftime("%H:%M:%S"),
                           'id': pushobj.id,
                           'user': pushobj.user,
                           'revision': tiprev,
                           'revdesc': tipdesc,
                           'status': 'green',
                           'compare': compare,
                           'signoff': cur,
                           'url': '%spushloghtml?changeset=%s' % (pushobj.repository.url, tiprev),
                           'accepted': current_accepted})
    return pushes

def _get_signoff_js(so):
    signoff = {}
    if so:
        signoff['when'] = so.when.strftime("%Y-%m-%d %H:%M")
        signoff['author'] = str(so.author)
        signoff['status'] = None if so.status==0 else so.accepted
        signoff['id'] = str(so.id)
        signoff['class'] = so.flag
        try:
            actions = Action.objects.filter(signoff=so).order_by('-pk')
            latest_action = actions[0]
            signoff['comment'] = latest_action.comment
        except IndexError:
            pass
    return signoff

def _get_notes(session):
    notes = {}
    for i in ('info','warning','error'):
        notes[i] = session.get('signoff_%s' % (i,), None)
        if notes[i]:
            del session['signoff_%s' % (i,)]
        else:
            del notes[i]
    return notes

def _get_push_offset(repo_url, id, shift=0, branches=None):
    """returns an offset of the push for signoff slider"""
    pushobjs = _get_pushes(branches)
    if not id:
        return 0
    push = pushobjs.get(changesets__revision__startswith=id, repository__url=repo_url)
    num = pushobjs.filter(pk__gt=push.pk, repository__url=repo_url).count()
    if num+shift<0:
        return 0
    return num+shift

def _get_accepted_signoff(locale, ms=None, av=None):
    '''this function gets the latest accepted signoff
    for a milestone/locale
    '''

    return _signoffs(ms is None and av or ms, locale=locale.code)


def _signoffs(appver_or_ms=None, status=1, getlist=False, locale=None):
    '''Get the signoffs for a milestone, or for the appversion as
    queryset (or manager).
    By default, returns the accepted ones, which can be overwritten to
    get any (status=None) or a particular status.

    If the locale argument is given, return the latest signoff with the
    requested status, or None. Requires appver_or_ms to be given.

    If getlist=True is specified, returns a dictionary mapping 
    tree-locale typles to a list of statuses, all that are newer than the
    latest obsolete action or accepted signoff (the latter is included).
    '''
    if isinstance(appver_or_ms, Milestone):
        ms = appver_or_ms
        if ms.status==2:
            assert not getlist
            if locale is not None:
                try:
                    return ms.signoffs.get(locale__code=locale)
                except Signoff.DoesNotExist:
                    return None
            return ms.signoffs
        appver = ms.appver
    else:
        appver = appver_or_ms

    sos = Signoff.objects
    if appver is not None:
        sos = sos.filter(appversion=appver)
    if locale is not None:
        sos = sos.filter(locale__code=locale)
    sos = sos.annotate(latest_action=Max('action__id'))
    sos_vals = list(sos.values('locale__code','id','latest_action', 'appversion__tree__code'))
    actions = Action.objects
    actionflags=dict(actions.filter(id__in=map(lambda d: d['latest_action'],
                                               sos_vals)).values_list('id','flag'))
    actionflags[0] = 0 # migrated pending signoffs lack any action :-(
    if getlist:
        lf = defaultdict(list)
    else:
        lf = dict()
    for d in sos_vals:
        loc = d['locale__code']
        tree = d['appversion__tree__code']
        flag = actionflags[d['latest_action'] or 0]
        if flag == 4 and status != 4:
            # obsoleted, drop previous signoffs
            lf.pop((tree,loc), None)
        else:
            if getlist:
                if flag == 1:
                    # approved, forget previous
                    lf[(tree,loc)] = [flag]
                else:
                    lf[(tree,loc)].append(flag)
            else:
                if status is not None:
                    if flag == status:
                        lf[(tree,loc)] = d['id']
                else:
                    lf[(tree,loc)] = d['id']

    if getlist:
        if locale is not None:
            for tree, loc in lf.iterkeys():
                if loc != locale:
                    lf.pop((tree,loc))
        return lf
    if locale is not None:
        assert appver
        try:
            return sos.get(id=lf[(appver.tree.code,locale)])
        except KeyError:
            return None
    return sos.filter(id__in=lf.values())
