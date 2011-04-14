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

"""Views centric around AppVersion data.
"""

from django.shortcuts import get_object_or_404, render_to_response
from django.http import HttpResponseRedirect

from shipping.models import *
from todo.views import snippets

def project_overview(request, appver_code):
    appver = get_object_or_404(AppVersion, code=appver_code)
    active_runs = appver.tree.run_set.filter(active__isnull=False)
    locales_ids = list(active_runs.values_list('locale', flat=True))
    locales = Locale.objects.filter(id__in=locales_ids)
    return render_to_response('shipping/project_overview.html',
                              {'appver': appver,
                               'locales': locales,})

def project_tasks(request, appver_code):
    appver = get_object_or_404(AppVersion, code=appver_code)
    tree = snippets.tree(request, tracker=None, project=appver.todo, 
                         task_view='shipping.views.task',
                         tracker_view='shipping.views.tracker')
    return render_to_response('shipping/project_tasks.html',
                              {'appver': appver,
                               'tree': tree,
                               # these are needed to make the log-in form 
                               # reload the page
                               'request': request,
                               'login_form_needs_reload': True,})

def combined_overview(request):
    if 'av' not in request.GET and 'locale' not in request.GET:
        raise Exception("No appversion nor locale passed as query args.")
    appver = get_object_or_404(AppVersion, code=request.GET['av'])
    locale = get_object_or_404(Locale, code=request.GET['locale'])
    tasks_showcase = snippets.showcase(request, appver.todo, locale,
                                       task_view='shipping.views.task')
    return render_to_response('shipping/combined_overview.html',
                              {'appver': appver,
                               'locale': locale,
                               'tasks_showcase': tasks_showcase,})

def combined_tasks(request):
    if 'av' not in request.GET and 'locale' not in request.GET:
        raise Exception("No appversion nor locale passed as query args.")
    appver = get_object_or_404(AppVersion, code=request.GET['av'])
    locale = get_object_or_404(Locale, code=request.GET['locale'])
    tree = snippets.tree(request, tracker=None, project=appver.todo,
                         locale=locale,
                         task_view='shipping.views.task',
                         tracker_view='shipping.views.tracker')
    return render_to_response('shipping/combined_tasks.html',
                              {'appver': appver,
                               'locale': locale,
                               'tree': tree,
                               # these are needed to make the log-in form 
                               # reload the page
                               'request': request,
                               'login_form_needs_reload': True,})

def changes(req, app_code):
    """Show which milestones on the given appversion took changes for which
    locale
    """
    try:
        av = AppVersion.objects.get(code=app_code)
    except AppVersion.DoesNotExist:
        # TODO: Hook up to a view that links to this
        return HttpResponseRedirect('/')

    ms_names = {}
    ms_codes = {}
    for ms in Milestone.objects.filter(appver=av).select_related('appver__app'):
        ms_names[ms.id] = str(ms)
        ms_codes[ms.id] = ms.code
    rows = []
    changes = None
    ms_id = None
    latest = {}
    current = {}
    ms_name = None
    for _mid, loc, pid in Milestone_Signoffs.objects.filter(milestone__appver=av).order_by('milestone__id','signoff__locale__code').values_list('milestone__id','signoff__locale__code','signoff__push__id'):
        if _mid != ms_id:
            ms_id = _mid
            # next milestone, bootstrap new row
            if latest:
                # previous milestone has locales left, update previous changes
                changes += [(_loc, 'dropped') for _loc in latest.iterkeys()]
                changes.sort(key=lambda t: t[0])
            latest = current
            current = {}
            ms_name = ms_names[ms_id]
            changes = []
            rows.append({'name': ms_name,
                         'code': ms_codes[ms_id],
                         'changes': changes})
        if loc not in latest:
            changes.append((loc, 'added'))
        else:
            lpid = latest.pop(loc)
            if lpid != pid:
                changes.append((loc, 'changed'))
        current[loc] = pid
    # see if we have some locales dropped in the last milestone
    if latest:
        # previous milestone has locales left, update previous changes
        changes += [(loc, 'dropped') for loc in latest.iterkeys()]
        changes.sort(key=lambda t: t[0])

    return render_to_response('shipping/app-changes.html',
                              {'appver': av,
                               'rows': rows,
                               })
