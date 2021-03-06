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
#   Peter Bengtsson <peterbe@mozilla.com>
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

import datetime
from nose.tools import eq_, ok_
from django.core.urlresolvers import reverse
from apps.shipping.tests import ShippingTestCaseBase
from apps.life.models import Tree, Locale
from apps.mbdb.models import Build
from models import Run, Active
from commons.tests.mixins import EmbedsTestCaseMixin


class L10nstatsTestCase(ShippingTestCaseBase, EmbedsTestCaseMixin):
    fixtures = ['one_started_l10n_build.json']

    def _create_active_run(self):
        locale, __ = Locale.objects.get_or_create(
          code='en-US',
          name='English',
        )
        tree = Tree.objects.all()[0]
        build = Build.objects.all()[0]
        run = Run.objects.create(
          locale=locale,
          tree=tree,
          build=build,
          srctime=datetime.datetime.now(),
        )
        Active.objects.create(run=run)
        return run

    def test_history_static_files(self):
        """render the tree_status view and check that all static files are
        accessible"""
        appver, milestone = self._create_appver_milestone()
        url = reverse('l10nstats.views.history_plot')
        response = self.client.get(url)
        eq_(response.status_code, 404)
        tree, = Tree.objects.all()
        locale, __ = Locale.objects.get_or_create(
          code='en-US',
          name='English',
        )
        data = {'tree': tree.code, 'locale': locale.code}
        response = self.client.get(url, data)
        eq_(response.status_code, 200)
        self.assert_all_embeds(response.content)

    def test_tree_status_static_files(self):
        """render the tree_status view and check that all static files are
        accessible"""
        appver, milestone = self._create_appver_milestone()

        url = reverse('l10nstats.views.tree_progress', args=['XXX'])
        response = self.client.get(url)
        eq_(response.status_code, 404)

        # _create_appver_milestone() creates a mock tree
        tree, = Tree.objects.all()
        url = reverse('l10nstats.views.tree_progress', args=[tree.code])
        response = self.client.get(url)
        eq_(response.status_code, 200)
        ok_('no statistics for %s' % tree.code in response.content)

        self._create_active_run()
        response = self.client.get(url)
        eq_(response.status_code, 200)

        self.assert_all_embeds(response.content)

    def test_render_index_static_files(self):
        """make sure all static files can be reached on the l10nstats index
        page"""
        url = reverse('l10nstats.views.index')
        response = self.client.get(url)
        eq_(response.status_code, 200)
        self.assert_all_embeds(response.content)

    def test_index_with_wrong_args(self):
        """index() view takes arguments 'locale' and 'tree' and if these
        aren't correct that view should raise a 404"""
        url = reverse('l10nstats.views.index')
        response = self.client.get(url, {'locale': 'xxx'})
        eq_(response.status_code, 404)

        locale, __ = Locale.objects.get_or_create(
          code='en-US',
          name='English',
        )

        locale, __ = Locale.objects.get_or_create(
          code='jp',
          name='Japanese',
        )

        response = self.client.get(url, {'locale': ['en-US', 'xxx']})
        eq_(response.status_code, 404)

        response = self.client.get(url, {'locale': ['en-US', 'jp']})
        eq_(response.status_code, 200)

        # test the tree argument now
        response = self.client.get(url, {'tree': 'xxx'})
        eq_(response.status_code, 404)

        self._create_appver_milestone()
        assert Tree.objects.all().exists()
        tree, = Tree.objects.all()

        response = self.client.get(url, {'tree': ['xxx', tree.code]})
        eq_(response.status_code, 404)

        response = self.client.get(url, {'tree': [tree.code]})
        eq_(response.status_code, 200)
