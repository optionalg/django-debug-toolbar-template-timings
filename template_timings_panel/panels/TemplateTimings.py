from debug_toolbar.panels import DebugPanel
from django.conf import settings
from django.template.base import Template
from django.template.loader_tags import BlockNode
from debug_toolbar.utils.tracking import replace_call
from debug_toolbar.panels import sql
from django.core.exceptions import ImproperlyConfigured
import threading
import functools
import time
import re
import collections
import logging

logger = logging.getLogger(__name__)

if not "debug_toolbar.panels.sql.SQLDebugPanel" in settings.DEBUG_TOOLBAR_PANELS:
    raise ImproperlyConfigured("debug_toolbar.panels.sql.SQLDebugPanel must be present in DEBUG_TOOLBAR_PANELS")

def record_query(**kwargs):
    if hasattr(results, "_current_template"):
        if not results._current_key in results.timings or \
                not results._current_template in results.timings[results._current_key]:
            return

        part = results.timings[results._current_key][results._current_template]
        part["queries"] += 1
        part["query_duration"] += kwargs["duration"]
        logger.debug("Template: %s executed query %s" % (results._current_template, kwargs["raw_sql"]))


@replace_call(sql.SQLDebugPanel.record)
def record(func, self, **kwargs):
    record_query(**kwargs)
    return func(self, **kwargs)


results = threading.local()

TEMPLATE_TIMINGS_SETTINGS = {
    'IGNORED_TEMPLATES': ["debug_toolbar/*"]
}

for k in TEMPLATE_TIMINGS_SETTINGS.keys():
    if hasattr(settings, k):
        TEMPLATE_TIMINGS_SETTINGS[k] = getattr(settings, k)


def _template_render_wrapper(func, key, should_add=lambda n: True, name=lambda s: s.name if s.name else ''):

    @functools.wraps(func)
    def timing_hook(self, *args, **kwargs):
        # Set up our thread-local results cache
        if not hasattr(results, "timings"):
            results.timings = collections.defaultdict(dict)
            # This is like a stack. It gets incremented before a template is rendered and decremented when
            # its finished rendering, because this function is recursive. So if it is 1 then this is the first
            # template being rendered, and its total execution time encompasses all of the sub-templates rendering
            # time. We use this to display the rendering time on the sidebar
            results._count = 0
            # These two values are used with the SQL counter. We store the current key and template name
            # so that the _logger can get to the current template being rendered.
            results._current_template = name(self)
            results._current_key = key

        name_self = name(self)

        # Issue #11, sometimes for some reason accessing results.timings causes a KeyError.
        if key not in results.timings:
            results.timings[key] = {}

        if name_self not in results.timings[key] and should_add(name_self):
            results.timings.setdefault(key, {})[name_self] = {
                'count': 0,
                'min': None,
                'max': None,
                'total': 0,
                'avg': 0,
                'is_base': False,
                'queries': 0,
                'query_duration': 0
            }

        results._count += 1
        _old_current = results._current_template
        _old_key = results._current_key

        results._current_template = name_self
        results._current_key = key

        start_time = time.time()
        result = func(self, *args, **kwargs)
        time_taken = (time.time() - start_time) * 1000.0

        results._current_template = _old_current
        results._current_key = _old_key

        if should_add(name_self):
            results_part = results.timings[key][name_self]
            results_part["min"] = min(time_taken, results_part["min"])
            results_part["max"] = max(time_taken, results_part["max"])

            results_part['count'] += 1
            results_part['total'] += time_taken
            results_part['avg'] = results_part['total'] / results_part['count']
            results_part["is_base"] = results._count == 1
            if results_part["queries"] > 0:
                try:
                    results_part["sql_percentage"] = "%.2f%%" % ((float(results_part["query_duration"]) / float(results_part["total"])) * 100)
                except ZeroDivisionError:
                    results_part["sql_percentage"] = "0%"

            logger.debug("%s %s took %.1f" % (key, name_self, time_taken))

        results._count -= 1
        return result

    return timing_hook

Template.render = _template_render_wrapper(Template.render, "templates",
                                           lambda n: not any([re.match(pattern, n)
                                                              for pattern in TEMPLATE_TIMINGS_SETTINGS["IGNORED_TEMPLATES"]]))
BlockNode.render = _template_render_wrapper(BlockNode.render, "blocks")
#IncludeNode.render = _template_render_wrapper(IncludeNode.render, "includes", name=lambda s: s.template_name)
#ConstantIncludeNode.render = _template_render_wrapper(ConstantIncludeNode.render, "includes",
#                                                      name=lambda s: s.template.name)


class TemplateTimings(DebugPanel):
    name = "TemplateTimingPanel"
    template = "debug_toolbar_template_timings.html"
    has_content = True

    def _get_timings(self):
        return getattr(results, "timings", None)

    def nav_title(self):
        return 'Template Timings'

    def nav_subtitle(self):
        results = self._get_timings()

        total_template_queries = sum(
            sum(results[name][template_name]["queries"] for template_name in results[name])
            for name in results
        )

        total_template_query_time = sum(
            sum(results[name][template_name]["query_duration"] for template_name in results[name])
            for name in results
        )

        base_template = filter(lambda i: results["templates"][i]["is_base"] == True, results["templates"].keys())

        if not len(base_template) == 1:
            logger.info("Found more than one base template: %s" % str(base_template))
        else:
            base_template = base_template[0]
            base_time = results["templates"][base_template]["total"]
            query_percentage_time = ""
            if total_template_query_time > 0 and base_time > 0:
                query_percentage_time = "(%.2f%% SQL)" % ((float(total_template_query_time) / float(base_time)) * 100)

            return "%.0f ms with %s queries %s" % (base_time, total_template_queries, query_percentage_time)

    def title(self):
        return 'Template Timings'

    def url(self):
        return ''

    def process_response(self, request, response):
        timings = self._get_timings()
        # Setting default_factory to None allows us to access
        # template_timings.iteritems in the template.
        if timings is not None:
            timings.default_factory = None
        self.record_stats({"template_timings": timings})
