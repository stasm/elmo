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

from django.shortcuts import render_to_response, get_object_or_404
from django.template import RequestContext
from django.template.loader import render_to_string
from django.http import (HttpResponseRedirect, HttpResponse, Http404,
                         HttpResponseNotAllowed, HttpResponseForbidden)
from life.models import Repository, Locale
from shipping.models import Milestone, Signoff, AppVersion, Action
from shipping.api import (signoff_actions, flag_lists, accepted_signoffs,
                          signoff_summary, annotated_pushes)
from django.conf import settings
from django.core.urlresolvers import reverse
from django.views.decorators.cache import cache_control
from django.utils import simplejson
from django.utils.http import urlencode
from django.utils.safestring import mark_safe
from django.db.models import Max

from collections import defaultdict
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
    }, context_instance=RequestContext(request))

def homesnippet(request):
    q = AppVersion.objects.filter(milestone__status=1).select_related('app')
    q = q.order_by('app__name','-version')
    return render_to_string('shipping/snippet.html', {
            'appvers': q,
            })


def teamsnippet(request, loc):
    runs = loc.run_set.filter(active__isnull=False).select_related('tree') \
                       .order_by('tree__code')
    for run in runs:
        run.changed_ratio = run.completion
        run.unchanged_ratio = 100 * run.unchanged / run.total
        run.missing_ratio = 100 * run.allmissing / run.total
        # cheat a bit and make sure that the red stripe on the chart is at 
        # least 1px wide
        if run.allmissing and run.missing_ratio == 0:
            run.missing_ratio = 1
            for ratio in (run.changed_ratio, run.unchanged_ratio):
                if ratio:
                    ratio = ratio - 1
                    break
        try:
            run.appversion = run.tree.appversion_set.all()[0]
        except IndexError:
            run.appversion = None
            continue

        actions = [action_id for action_id, flag
                   in signoff_actions(appversions={'id': run.appversion.id},
                                      locales={'id': loc.id})]
        actions = Action.objects.filter(id__in=actions) \
                                .select_related('signoff__push')
        # get current status of signoffs
        run.pending, run.rejected, run.accepted, _ = signoff_summary(actions)

        # get the suggested signoff
        forest = run.tree.l10n
        repo = get_object_or_404(Repository, locale=loc, forest=forest)
        _, _, run.suggested_shortrev = annotated_pushes(repo, run.appversion, 
                                                        loc, actions)

    return render_to_string('shipping/team-snippet.html',
                            {'locale': loc,
                             'runs': runs,
                            }, context_instance=RequestContext(request))

def _universal_newlines(content):
    "CompareLocales reads files with universal newlines, fake that"
    return content.replace('\r\n','\n').replace('\r','\n')

def diff_app(request):
    # XXX TODO: error handling
    if 'repo' not in request.GET:
        raise Http404("repo must be supplied")
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
    paths = ([(f, 'changed') for f in changed]
             + [(f, 'removed') for f in removed]
             + [(f, 'added') for f in added])
    diffs = DataTree(dict)
    for path, action in paths:
        lines = []
        try:
            p = getParser(path)
        except UserWarning:
            diffs[path].update({'path': path,
                                'isFile': True,
                                'rev': ((action == 'removed') and request.GET['from']
                                        or request.GET['to']),
                                'class': action})
            continue
        if action == 'added':
            a_entities = []
            a_map = {}
        else:
            data = ctx1.filectx(path).data()
            data = _universal_newlines(data)
            try:
                p.readContents(data)
                a_entities, a_map = p.parse()
            except:
                diffs[path].update({'path': path,
                                    'isFile': True,
                                    'rev': ((action == 'removed') and request.GET['from']
                                            or request.GET['to']),
                                    'class': action})
                continue

        if action == 'removed':
            c_entities = []
            c_map = {}
        else:
            data = ctx2.filectx(path).data()
            data = _universal_newlines(data)
            try:
                p.readContents(data)
                c_entities, c_map = p.parse()
            except:
                diffs[path].update({'path': path,
                                    'isFile': True,
                                    'rev': ((action == 'removed') and request.GET['from']
                                            or request.GET['to']),
                                    'class': action})
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
                               'diffs': diffs},
                               context_instance=RequestContext(request))


def dashboard(request):
    args = [] # params to pass to l10nstats json
    query = [] # params to pass to shipping json
    subtitles = []
    if 'ms' in request.GET:
        mstone = get_object_or_404(Milestone, code=request.GET['ms'])
        args.append(('tree', mstone.appver.tree.code))
        subtitles.append(str(mstone))
        query.append(('ms', mstone.code))
    elif 'av' in request.GET:
        appver = get_object_or_404(AppVersion, code=request.GET['av'])
        args.append(('tree', (appver.tree is not None and appver.tree.code)
                     or appver.lasttree.code))
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
            }, context_instance=RequestContext(request))


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
    if not request.GET.get('ms'):
        raise Http404("ms must be supplied")
    mstone = get_object_or_404(Milestone, code=request.GET['ms'])
    if mstone.status != Milestone.OPEN:
        return HttpResponseRedirect(reverse('shipping.views.milestones'))
    statuses = flag_lists(appversions={'id': mstone.appver_id})
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

    Redirects to milestones().
    """
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    if not request.user.has_perm('shipping.can_ship'):
        # XXX: personally I'd prefer if this was a raised 4xx error (peter)
        # then I can guarantee better test coverage
        return HttpResponseRedirect(reverse('shipping.views.milestones'))

    mstone = get_object_or_404(Milestone, code=request.POST['ms'])
    if mstone.status != Milestone.OPEN:
        return HttpResponseForbidden('Can only ship open milestones')
    cs = (accepted_signoffs(id=mstone.appver_id)
          .values_list('id', flat=True))
    mstone.signoffs.add(*list(cs))  # add them
    mstone.status = 2
    # XXX create event
    mstone.save()

    return HttpResponseRedirect(reverse('shipping.views.milestones'))


def confirm_drill_mstone(request):
    """Intermediate page when fire-drilling a milestone.

    Gathers all data to verify when shipping.
    Ends up in drill_mstone if everything is fine.
    Redirects to milestones() in case of trouble.
    """
    if not request.GET.get('ms'):
        raise Http404("ms must be supplied")
    if not request.user.has_perm('shipping.can_ship'):
        return HttpResponseRedirect(reverse('shipping.views.milestones'))
    mstone = get_object_or_404(Milestone, code=request.GET['ms'])
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
