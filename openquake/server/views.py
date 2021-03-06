# Copyright (c) 2015, GEM Foundation.
#
# This program is free software: you can redistribute it and/or modify
# under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import shutil
import json
import logging
import os
import traceback
import tempfile
import urlparse

from xml.etree import ElementTree as etree

from django.core.exceptions import ObjectDoesNotExist
from django.http import HttpResponse
from django.http import HttpResponseNotFound
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods
from django.shortcuts import render_to_response
from django.template import RequestContext

from openquake.baselib.general import groupby
from openquake.commonlib import nrml, readinput, valid
from openquake.engine import engine as oq_engine, __version__ as oqversion
from openquake.engine.db import models as oqe_models
from openquake.engine.export import core
from openquake.engine.utils.tasks import safely_call
from openquake.engine.export.core import export_output
from openquake.server import tasks, executor, utils

METHOD_NOT_ALLOWED = 405
NOT_IMPLEMENTED = 501
JSON = 'application/json'

DEFAULT_LOG_LEVEL = 'progress'

#: For exporting calculation outputs, the client can request a specific format
#: (xml, geojson, csv, etc.). If the client does not specify give them (NRML)
#: XML by default.
DEFAULT_EXPORT_TYPE = 'xml'

EXPORT_CONTENT_TYPE_MAP = dict(xml='application/xml',
                               geojson='application/json')
DEFAULT_CONTENT_TYPE = 'text/plain'

LOGGER = logging.getLogger('openquake.server')

ACCESS_HEADERS = {'Access-Control-Allow-Origin': '*',
                  'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
                  'Access-Control-Max-Age': 1000,
                  'Access-Control-Allow-Headers': '*'}


# Credit for this decorator to https://gist.github.com/aschem/1308865.
def cross_domain_ajax(func):
    def wrap(request, *args, **kwargs):
        # Firefox sends 'OPTIONS' request for cross-domain javascript call.
        if not request.method == "OPTIONS":
            response = func(request, *args, **kwargs)
        else:
            response = HttpResponse()
        for k, v in ACCESS_HEADERS.iteritems():
            response[k] = v
        return response
    return wrap


def _get_base_url(request):
    """
    Construct a base URL, given a request object.

    This comprises the protocol prefix (http:// or https://) and the host,
    which can include the port number. For example:
    http://www.openquake.org or https://www.openquake.org:8000.
    """
    if request.is_secure():
        base_url = 'https://%s'
    else:
        base_url = 'http://%s'
    base_url %= request.META['HTTP_HOST']
    return base_url


def _prepare_job(request, hazard_output_id, hazard_job_id,
                 candidates):
    """
    Creates a temporary directory, move uploaded files there and
    select the job file by looking at the candidate names.

    :returns: full path of the job_file
    """
    temp_dir = tempfile.mkdtemp()
    inifiles = []
    arch = request.FILES.get('archive')
    if arch is None:
        # move each file to a new temp dir, using the upload file names,
        # not the temporary ones
        for each_file in request.FILES.values():
            new_path = os.path.join(temp_dir, each_file.name)
            shutil.move(each_file.temporary_file_path(), new_path)
            if each_file.name in candidates:
                inifiles.append(new_path)
        return inifiles
    # else extract the files from the archive into temp_dir
    return readinput.extract_from_zip(arch, candidates)


def _is_source_model(tempfile):
    """
    Return true if an uploaded NRML file is a seismic source model.
    """
    tree = etree.iterparse(tempfile, events=('start', 'end'))
    # pop off the first elements, which should be a <nrml> element
    # and something else
    _, nrml_elem = tree.next()
    _, model_elem = tree.next()

    assert nrml_elem.tag == '{%s}nrml' % nrml.NAMESPACE, (
        "Input file is not a NRML artifact"
    )

    if model_elem.tag == '{%s}sourceModel' % nrml.NAMESPACE:
        return True
    return False


@cross_domain_ajax
@require_http_methods(['GET'])
def get_engine_version(request):
    """
    Return a string with the openquake.engine version
    """
    return HttpResponse(oqversion)


@require_http_methods(['GET'])
@cross_domain_ajax
def calc_info(request, calc_id):
    """
    Get a JSON blob containing all of parameters for the given calculation
    (specified by ``calc_id``). Also includes the current job status (
    executing, complete, etc.).
    """
    try:
        calc = oqe_models.OqJob.objects.get(pk=calc_id)
        response_data = vars(calc.get_oqparam())
        response_data['status'] = calc.status
        response_data['start_time'] = str(calc.jobstats.start_time)
        response_data['stop_time'] = str(calc.jobstats.stop_time)
        response_data['is_running'] = calc.is_running

    except ObjectDoesNotExist:
        return HttpResponseNotFound()

    return HttpResponse(content=json.dumps(response_data), content_type=JSON)


@require_http_methods(['GET'])
@cross_domain_ajax
def calc(request, id=None):
    """
    Get a list of calculations and report their id, status, job_type,
    is_running, description, and a url where more detailed information
    can be accessed.

    Responses are in JSON.
    """
    base_url = _get_base_url(request)

    user_name = utils.getusername(request)

    calc_data = _get_calcs(request.GET, user_name, id=id)

    response_data = []
    for hc_id, status, job_type, is_running, desc in calc_data:
        url = urlparse.urljoin(base_url, 'v1/calc/%d' % hc_id)
        response_data.append(
            dict(id=hc_id, status=status, job_type=job_type,
                 is_running=is_running, description=desc, url=url))

    # if id is specified the related dictionary is returned instead the list
    if id is not None:
        [response_data] = response_data

    return HttpResponse(content=json.dumps(response_data),
                        content_type=JSON)


@csrf_exempt
@cross_domain_ajax
@require_http_methods(['POST'])
def calc_remove(request, calc_id):
    """
    Remove the calculation id by setting the field oq_job.relevant to False.
    """
    try:
        job = oqe_models.OqJob.objects.get(pk=calc_id)
    except ObjectDoesNotExist:
        return HttpResponseNotFound()
    try:
        job.relevant = False
        job.save()
    except:
        response_data = traceback.format_exc().splitlines()
        status = 500
    else:
        response_data = []
        status = 200
    return HttpResponse(content=json.dumps(response_data),
                        content_type=JSON, status=status)


def log_to_json(log):
    """Convert a log record into a list of strings"""
    return [log.timestamp.isoformat()[:22],
            log.level, log.process, log.message]


@require_http_methods(['GET'])
@cross_domain_ajax
def get_log_slice(request, calc_id, start, stop):
    """
    Get a slice of the calculation log as a JSON list of rows
    """
    start = start or 0
    stop = stop or None
    try:
        rows = oqe_models.Log.objects.filter(job_id=calc_id)[start:stop]
        response_data = map(log_to_json, rows)
    except ObjectDoesNotExist:
        return HttpResponseNotFound()
    return HttpResponse(content=json.dumps(response_data), content_type=JSON)


@require_http_methods(['GET'])
@cross_domain_ajax
def get_log_size(request, calc_id):
    """
    Get the current number of lines in the log
    """
    try:
        response_data = oqe_models.Log.objects.filter(job_id=calc_id).count()
    except ObjectDoesNotExist:
        return HttpResponseNotFound()
    return HttpResponse(content=json.dumps(response_data), content_type=JSON)


@csrf_exempt
@cross_domain_ajax
@require_http_methods(['POST'])
def run_calc(request):
    """
    Run a calculation.

    :param request:
        a `django.http.HttpRequest` object.
    """
    callback_url = request.POST.get('callback_url')
    foreign_calc_id = request.POST.get('foreign_calculation_id')

    hazard_output_id = request.POST.get('hazard_output_id')
    hazard_job_id = request.POST.get('hazard_job_id')

    is_risk = hazard_output_id or hazard_job_id
    if is_risk:
        candidates = ("job_risk.ini", "job.ini")
    else:
        candidates = ("job_hazard.ini", "job.ini")
    einfo, exctype = safely_call(
        _prepare_job, (request, hazard_output_id, hazard_job_id, candidates))
    if exctype:
        tasks.update_calculation(callback_url, status="failed", einfo=einfo)
        return HttpResponse(json.dumps(einfo.splitlines()),
                            content_type=JSON, status=500)
    if not einfo:
        msg = 'Could not find any file of the form %s' % str(candidates)
        logging.error(msg)
        return HttpResponse(content=json.dumps([msg]), content_type=JSON,
                            status=500)

    temp_dir = os.path.dirname(einfo[0])

    user_name = utils.getusername(request)

    try:
        job, _fut = submit_job(einfo[0], temp_dir, request.POST['database'],
                               user_name, callback_url, foreign_calc_id,
                               hazard_output_id, hazard_job_id)
    except Exception as exc:  # no job created, for instance missing .xml file
        logging.error(exc)
        response_data = str(exc).splitlines()
        status = 500
    else:
        calc = oqe_models.OqJob.objects.get(pk=job.id)
        response_data = vars(calc.get_oqparam())
        response_data['job_id'] = job.id
        response_data['status'] = calc.status
        status = 200
    return HttpResponse(content=json.dumps(response_data), content_type=JSON,
                        status=status)


def submit_job(job_file, temp_dir, dbname, user_name,
               callback_url=None, foreign_calc_id=None,
               hazard_output_id=None, hazard_job_id=None,
               logfile=None):
    """
    Create a job object from the given job.ini file in the job directory
    and submit it to the job queue.
    """
    ini = os.path.join(temp_dir, job_file)
    job, exctype = safely_call(
        oq_engine.job_from_file, (ini, user_name, DEFAULT_LOG_LEVEL, '',
                                  hazard_output_id, hazard_job_id))
    if exctype:
        tasks.update_calculation(callback_url, status="failed", einfo=job)
        raise exctype(job)

    future = executor.submit(
        tasks.safely_call, tasks.run_calc, job.id, temp_dir,
        callback_url, foreign_calc_id, dbname, logfile)
    return job, future


def _get_calcs(request_get_dict, user_name, id=None):

    # TODO if superuser with should show all the calculations i.e.

    # helper to get job+calculation data from the oq-engine database
    job_params = oqe_models.JobParam.objects.filter(
        name='description', job__user_name=user_name).order_by('-id')

    if id is not None:
        job_params = job_params.filter(job_id=id)

    if 'job_type' in request_get_dict:
        job_type = request_get_dict.get('job_type')
        job_params = job_params.filter(
            job__hazard_calculation__isnull=job_type == 'hazard')

    if 'is_running' in request_get_dict:
        is_running = request_get_dict.get('is_running')
        job_params = job_params.filter(
            job__is_running=valid.boolean(is_running))

    if 'relevant' in request_get_dict:
        relevant = request_get_dict.get('relevant')
        job_params = job_params.filter(job__relevant=valid.boolean(relevant))

    return [(jp.job.id, jp.job.status, jp.job.job_type, jp.job.is_running,
             jp.value) for jp in job_params]


@require_http_methods(['GET'])
@cross_domain_ajax
def calc_results(request, calc_id):
    """
    Get a summarized list of calculation results for a given ``calc_id``.
    Result is a JSON array of objects containing the following attributes:

        * id
        * name
        * type (hazard_curve, hazard_map, etc.)
        * url (the exact url where the full result can be accessed)
    """
    user_name = utils.getusername(request)

    # If the specified calculation doesn't exist OR is not yet complete,
    # throw back a 404.
    try:
        oqjob = oqe_models.OqJob.objects.get(id=calc_id,
                                             user_name=user_name)
        if not oqjob.status == 'complete':
            return HttpResponseNotFound()
    except ObjectDoesNotExist:
        return HttpResponseNotFound()
    base_url = _get_base_url(request)

    # NB: export_output has as keys the list (output_type, extension)
    # so this returns an ordered map output_type -> extensions such as
    # OrderedDict([('agg_loss_curve', ['xml', 'csv']), ...])
    output_types = groupby(export_output, lambda oe: oe[0],
                           lambda oes: [e for o, e in oes])

    results = oq_engine.get_outputs(calc_id)
    if not results:
        return HttpResponseNotFound()

    response_data = []
    for result in results:
        url = urlparse.urljoin(base_url, 'v1/calc/result/%d' % result.id)
        datum = dict(
            id=result.id,
            name=result.display_name,
            type=result.output_type,
            outtypes=output_types[result.output_type],
            url=url,
        )
        response_data.append(datum)

    return HttpResponse(content=json.dumps(response_data))


@require_http_methods(['GET'])
@cross_domain_ajax
def get_traceback(request, calc_id):
    """
    Get the traceback as a list of lines for a given ``calc_id``.
    """
    # If the specified calculation doesn't exist throw back a 404.
    try:
        oqe_models.OqJob.objects.get(id=calc_id)
    except ObjectDoesNotExist:
        return HttpResponseNotFound()

    response_data = oqe_models.Log.objects.get(
        job_id=calc_id, level='CRITICAL').message.splitlines()
    return HttpResponse(content=json.dumps(response_data), content_type=JSON)


@cross_domain_ajax
@require_http_methods(['GET'])
def get_result(request, result_id):
    """
    Download a specific result, by ``result_id``.

    The common abstracted functionality for getting hazard or risk results.

    :param request:
        `django.http.HttpRequest` object. Can contain a `export_type` GET
        param (the default is 'xml' if no param is specified).
    :param result_id:
        The id of the requested artifact.
    :returns:
        If the requested ``result_id`` is not available in the format
        designated by the `export_type`.

        Otherwise, return a `django.http.HttpResponse` containing the content
        of the requested artifact.

    Parameters for the GET request can include an `export_type`, such as 'xml',
    'geojson', 'csv', etc.
    """
    # If the result for the requested ID doesn't exist, OR
    # the job which it is related too is not complete,
    # throw back a 404.
    try:
        output = oqe_models.Output.objects.get(id=result_id)
        job = output.oq_job
        if not job.status == 'complete':
            return HttpResponseNotFound()
    except ObjectDoesNotExist:
        return HttpResponseNotFound()

    etype = request.GET.get('export_type')
    export_type = etype or DEFAULT_EXPORT_TYPE

    dload = request.GET.get('dload')
    download = False
    if dload is None:
        if etype is not None:
            download = True
    else:
        if dload == "true":
            download = True

    tmpdir = tempfile.mkdtemp()
    exported = core.export(result_id, tmpdir, export_type=export_type)
    if exported is None:
        # Throw back a 404 if the exact export parameters are not supported
        return HttpResponseNotFound(
            'export_type=%s is not supported for output_type=%s' %
            (export_type, output.output_type))

    content_type = EXPORT_CONTENT_TYPE_MAP.get(
        export_type, DEFAULT_CONTENT_TYPE)
    try:
        fname = 'output-%s-%s' % (result_id, os.path.basename(exported))
        data = open(exported).read()
        response = HttpResponse(data, content_type=content_type)
        response['Content-Length'] = len(data)
        if download:  # download as a file
            response['Content-Disposition'] = 'attachment; filename=%s' % fname
        return response
    finally:
        shutil.rmtree(tmpdir)


def web_engine(request, **kwargs):
    return render_to_response("engine/index.html",
                              dict(),
                              context_instance=RequestContext(request))


@cross_domain_ajax
@require_http_methods(['GET'])
def web_engine_get_outputs(request, calc_id, **kwargs):
    return render_to_response("engine/get_outputs.html",
                              dict([('calc_id', calc_id)]),
                              context_instance=RequestContext(request))


@require_http_methods(['GET'])
def license(request, **kwargs):
    return render_to_response("engine/license.html",
                              context_instance=RequestContext(request))
