############################################################
#
# Vendored KBase Execution Engine 2 (EE2) client.
#
# Follows the same auto-generated client pattern used by
# WorkspaceClient.py and AbstractHandleClient.py.
#
############################################################

# the following is a hack to get the baseclient to import whether we're in a
# package or not. This makes pep8 unhappy hence the annotations.
try:
    # baseclient and this client are in a package
    from .baseclient import BaseClient as _BaseClient  # @UnusedImport
except ImportError:
    # no they aren't
    from baseclient import BaseClient as _BaseClient  # @Reimport


class execution_engine2:
    """Client for the KBase Execution Engine 2 (EE2) service.

    Provides methods for submitting, monitoring, and managing
    asynchronous jobs in the KBase platform.
    """

    def __init__(
        self,
        url=None,
        timeout=30 * 60,
        user_id=None,
        password=None,
        token=None,
        ignore_authrc=False,
        trust_all_ssl_certificates=False,
        auth_svc="https://ci.kbase.us/services/auth/api/legacy/KBase/Sessions/Login",
    ):
        if url is None:
            raise ValueError("A url is required")
        self._service_ver = None
        self._client = _BaseClient(
            url,
            timeout=timeout,
            user_id=user_id,
            password=password,
            token=token,
            ignore_authrc=ignore_authrc,
            trust_all_ssl_certificates=trust_all_ssl_certificates,
            auth_svc=auth_svc,
        )

    def list_config(self, context=None):
        """List the current configuration of the EE2 service.

        :returns: instance of mapping from String to String
        """
        return self._client.call_method(
            "execution_engine2.list_config", [], self._service_ver, context
        )

    def ver(self, context=None):
        """Returns the version of the execution_engine2 service.

        :returns: instance of String
        """
        return self._client.call_method(
            "execution_engine2.ver", [], self._service_ver, context
        )

    def status(self, context=None):
        """Returns the status of the execution_engine2 service.

        :returns: instance of type "Status"
        """
        return self._client.call_method(
            "execution_engine2.status", [], self._service_ver, context
        )

    def run_job(self, params, context=None):
        """Start a new job.

        :param params: instance of type "RunJobParams" -> structure
        :returns: instance of type "job_id" (A string for the job id)
        """
        return self._client.call_method(
            "execution_engine2.run_job", [params], self._service_ver, context
        )

    def run_job_batch(self, params, batch_params, context=None):
        """Run a batch of jobs.

        :param params: instance of list of type "RunJobParams"
        :param batch_params: instance of type "BatchParams"
        :returns: instance of type "RunJobBatchResult"
        """
        return self._client.call_method(
            "execution_engine2.run_job_batch",
            [params, batch_params],
            self._service_ver,
            context,
        )

    def run_job_concierge(self, params, concierge_params, context=None):
        """Run a job with concierge service.

        :param params: instance of type "RunJobParams"
        :param concierge_params: instance of type "ConciergeParams"
        :returns: instance of type "job_id"
        """
        return self._client.call_method(
            "execution_engine2.run_job_concierge",
            [params, concierge_params],
            self._service_ver,
            context,
        )

    def get_job_params(self, params, context=None):
        """Get the params of a job.

        :param params: instance of type "GetJobParams"
        :returns: instance of type "RunJobParams"
        """
        return self._client.call_method(
            "execution_engine2.get_job_params",
            [params],
            self._service_ver,
            context,
        )

    def check_job(self, params, context=None):
        """Check the status of a job.

        :param params: instance of type "CheckJobParams" -> structure:
            parameter "job_id" of type "job_id"
        :returns: instance of type "JobState" -> structure
        """
        return self._client.call_method(
            "execution_engine2.check_job",
            [params],
            self._service_ver,
            context,
        )

    def check_jobs(self, params, context=None):
        """Check the status of multiple jobs.

        :param params: instance of type "CheckJobsParams" -> structure:
            parameter "job_ids" of list of type "job_id"
        :returns: instance of type "CheckJobsResults"
        """
        return self._client.call_method(
            "execution_engine2.check_jobs",
            [params],
            self._service_ver,
            context,
        )

    def check_job_canceled(self, params, context=None):
        """Check whether a job has been canceled.

        :param params: instance of type "CancelJobParams"
        :returns: instance of type "CheckJobCanceledResult"
        """
        return self._client.call_method(
            "execution_engine2.check_job_canceled",
            [params],
            self._service_ver,
            context,
        )

    def get_job_status(self, params, context=None):
        """Get the status of a job.

        :param params: instance of type "GetJobStatusParams"
        :returns: instance of type "GetJobStatusResult"
        """
        return self._client.call_method(
            "execution_engine2.get_job_status",
            [params],
            self._service_ver,
            context,
        )

    def check_workspace_jobs(self, params, context=None):
        """Check the status of all jobs in a workspace.

        :param params: instance of type "CheckWorkspaceJobsParams"
        :returns: instance of type "CheckJobsResults"
        """
        return self._client.call_method(
            "execution_engine2.check_workspace_jobs",
            [params],
            self._service_ver,
            context,
        )

    def cancel_job(self, params, context=None):
        """Cancel a running job.

        :param params: instance of type "CancelJobParams" -> structure:
            parameter "job_id" of type "job_id"
        :returns: None
        """
        return self._client.call_method(
            "execution_engine2.cancel_job",
            [params],
            self._service_ver,
            context,
        )

    def get_job_logs(self, params, context=None):
        """Get the logs for a job.

        :param params: instance of type "GetJobLogsParams" -> structure:
            parameter "job_id" of type "job_id",
            parameter "skip_lines" of Long
        :returns: instance of type "GetJobLogsResults"
        """
        return self._client.call_method(
            "execution_engine2.get_job_logs",
            [params],
            self._service_ver,
            context,
        )

    def finish_job(self, params, context=None):
        """Mark a job as finished.

        :param params: instance of type "FinishJobParams"
        :returns: None
        """
        return self._client.call_method(
            "execution_engine2.finish_job",
            [params],
            self._service_ver,
            context,
        )

    def start_job(self, params, context=None):
        """Start a created job.

        :param params: instance of type "StartJobParams"
        :returns: None
        """
        return self._client.call_method(
            "execution_engine2.start_job",
            [params],
            self._service_ver,
            context,
        )

    def list_jobs(self, params, context=None):
        """List jobs matching the given filters.

        :param params: instance of type "ListJobsParams"
        :returns: instance of list of type "JobState"
        """
        return self._client.call_method(
            "execution_engine2.list_jobs",
            [params],
            self._service_ver,
            context,
        )
