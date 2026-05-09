"""KBase SDK utilities for working with KBase SDK environments and services."""

from typing import Any

from .kb_ws_utils import KBWSUtils


class KBSDKUtils(KBWSUtils):
    """Utilities for working with KBase SDK environments and common SDK operations.

    Provides methods for workspace operations, client management, file handling,
    report generation, and other SDK-specific functionality commonly used in
    KBase applications and services.
    """

    def __init__(self, **kwargs: Any) -> None:
        """Initialize KBase SDK utilities.

        Args:
            module_name: Name of the SDK module
            working_dir: Working directory (defaults to scratch from config)
            module_dir: Module directory path
            callback_url: Callback URL for SDK services
            **kwargs: Additional keyword arguments passed to SharedEnvironment
        """
        super().__init__(**kwargs)

    def build_dataframe_report(self, table, column_list):
        # Convert columns to this format:
        columns = []
        for item in column_list:
            columns.append({"data": item})
        # for index, row in table.iterrows():
        #    pass
        json_str = table.to_json(orient="records")
        # columns=column_list
        html_data = (
            """
<html>
<header>
    <link href="https://cdn.datatables.net/1.11.5/css/jquery.dataTables.min.css" rel="stylesheet">
</header>
<body>
<script src="https://code.jquery.com/jquery-3.6.0.slim.min.js" integrity="sha256-u7e5khyithlIdTpu22PHhENmPcRdFiHRjhAuHcs05RI=" crossorigin="anonymous"></script>
<script type="text/javascript" src="https://cdn.datatables.net/1.11.5/js/jquery.dataTables.min.js"></script>
<script>
    $(document).ready(function() {
        $('#example').DataTable( {
            "ajax": {
                "url": "data.json"
            },
            "columns": """
            + json.dumps(columns, indent=4)
            + """
        } );
    } );
</script>
</body>
</html>
"""
        )
        os.makedirs(self.working_dir + "/html", exist_ok=True)
        with open(self.working_dir + "/html/index.html", "w") as f:
            f.write(html_data)
        with open(self.working_dir + "/html/data.json", "w") as f:
            f.write(json_str)


# ── Composition-based implementation ─────────────────────────────────────

class KBSDKUtilsImpl:
    """Composition-based SDK utilities.

    Holds ``env`` and ``ws`` instead of inheriting from ``KBWSUtils``.
    Delegates all method calls to an internal legacy instance.
    """

    def __init__(self, env, ws, **kwargs):
        self._env = env
        self._ws = ws
        _kwargs = {
            "config_file": False,
            "token_file": None,
            "kbase_token_file": None,
        }
        try:
            _kwargs["token"] = env.get_token("kbase")
        except Exception:
            pass
        _kwargs.update(kwargs)
        self._delegate = KBSDKUtils(**_kwargs)

    @property
    def env(self):
        return self._env

    @property
    def ws(self):
        return self._ws

    def __getattr__(self, name):
        return getattr(self._delegate, name)
