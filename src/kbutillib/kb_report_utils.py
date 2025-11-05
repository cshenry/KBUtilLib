"""KBase HTML Report utilities for creating interactive HTML reports.

This module provides utilities for creating HTML reports that can interact with
KBase datastores and dynamic services. It includes both Python-side report creation
and JavaScript utilities for client-side KBase API interactions.
"""

import json
import os
from typing import Any, Dict, List, Optional, Union

from .kb_ws_utils import KBWSUtils


class KBReportUtils(KBWSUtils):
    """Utilities for creating KBase-compliant HTML reports.

    This class provides methods to create interactive HTML reports that can
    communicate with KBase services, including workspace operations and dynamic
    service calls.

    Example:
        >>> report_utils = KBReportUtils()
        >>> html_content = report_utils.create_html_report(
        ...     title="My Analysis Report",
        ...     content="<div>Analysis results...</div>",
        ...     include_kbase_js=True
        ... )
        >>> report_utils.save_html_report(
        ...     html_content=html_content,
        ...     report_name="analysis_report",
        ...     workspace_id=12345
        ... )
    """

    def __init__(
        self, kb_version: Optional[str] = "prod", max_retry: int = 3, **kwargs: Any
    ) -> None:
        """Initialize KBase Report utilities.

        Args:
            kb_version: KBase environment version ('prod', 'appdev', 'ci')
            max_retry: Maximum number of retries for workspace operations
            **kwargs: Additional arguments passed to parent class
        """
        super().__init__(kb_version=kb_version, max_retry=max_retry, **kwargs)
        self.report_files = []

    def get_kbase_js_library(self) -> str:
        """Generate JavaScript library code for KBase API interactions.

        Returns:
            JavaScript code as a string that provides utilities for:
            - Reading KBase authentication tokens from cookies
            - Making workspace service calls
            - Making dynamic service calls
            - Managing workspace references
        """
        js_code = """
/**
 * KBase Report Utilities - JavaScript Library
 * Provides utilities for interacting with KBase services from HTML reports
 */

class KBaseReportUtils {
    constructor(config = {}) {
        this.config = {
            workspaceUrl: config.workspaceUrl || 'https://kbase.us/services/ws',
            serviceWizardUrl: config.serviceWizardUrl || 'https://kbase.us/services/service_wizard',
            ...config
        };
        this.token = this.getKBaseToken();
        this.wsClient = null;
        if (this.token) {
            this.initializeClients();
        }
    }

    /**
     * Get KBase authentication token from browser cookies
     * @returns {string|null} KBase session token or null if not found
     */
    getKBaseToken() {
        const cookies = Object.fromEntries(
            document.cookie.split('; ').map((cookie) => {
                const parts = cookie.split('=');
                return [parts[0], parts.slice(1).join('=')];
            })
        );
        return cookies.kbase_session || cookies.kbase_session_backup || null;
    }

    /**
     * Initialize workspace and service clients
     */
    initializeClients() {
        // Workspace client will be initialized when needed
        this.wsClient = new WorkspaceClient(this.config.workspaceUrl, this.token);
    }

    /**
     * Check if user is authenticated
     * @returns {boolean} True if authenticated
     */
    isAuthenticated() {
        return this.token !== null;
    }

    /**
     * Get workspace object by reference
     * @param {string} ref - Workspace reference (wsid/objid/ver)
     * @returns {Promise<Object>} Workspace object data
     */
    async getObject(ref) {
        if (!this.isAuthenticated()) {
            throw new Error('Not authenticated. KBase token not found in cookies.');
        }
        return await this.wsClient.getObjects([{ref: ref}]);
    }

    /**
     * Get multiple workspace objects
     * @param {Array<Object>} objects - Array of object specifications
     * @returns {Promise<Array>} Array of workspace objects
     */
    async getObjects(objects) {
        if (!this.isAuthenticated()) {
            throw new Error('Not authenticated. KBase token not found in cookies.');
        }
        return await this.wsClient.getObjects(objects);
    }

    /**
     * List objects in a workspace
     * @param {number|string} workspace - Workspace ID or name
     * @param {Object} options - Additional options (type, includeMetadata, etc.)
     * @returns {Promise<Array>} List of object info
     */
    async listObjects(workspace, options = {}) {
        if (!this.isAuthenticated()) {
            throw new Error('Not authenticated. KBase token not found in cookies.');
        }
        const params = {
            includeMetadata: options.includeMetadata || 0,
            ...options
        };
        if (typeof workspace === 'number') {
            params.ids = [workspace];
        } else {
            params.workspaces = [workspace];
        }
        return await this.wsClient.listObjects(params);
    }

    /**
     * Get workspace information
     * @param {number|string} workspace - Workspace ID or name
     * @returns {Promise<Array>} Workspace info
     */
    async getWorkspaceInfo(workspace) {
        if (!this.isAuthenticated()) {
            throw new Error('Not authenticated. KBase token not found in cookies.');
        }
        const params = typeof workspace === 'number'
            ? {id: workspace}
            : {workspace: workspace};
        return await this.wsClient.getWorkspaceInfo(params);
    }

    /**
     * Call a dynamic service method
     * @param {string} serviceName - Name of the service
     * @param {string} method - Method name
     * @param {Array} params - Method parameters
     * @returns {Promise<any>} Service response
     */
    async callService(serviceName, method, params) {
        if (!this.isAuthenticated()) {
            throw new Error('Not authenticated. KBase token not found in cookies.');
        }
        const serviceUrl = await this.getServiceUrl(serviceName);
        return await this.makeJsonRpcCall(serviceUrl, method, params);
    }

    /**
     * Get service URL from service wizard
     * @param {string} serviceName - Name of the service
     * @returns {Promise<string>} Service URL
     */
    async getServiceUrl(serviceName) {
        const response = await this.makeJsonRpcCall(
            this.config.serviceWizardUrl,
            'ServiceWizard.get_service_status',
            [{module_name: serviceName, version: null}]
        );
        return response[0].url;
    }

    /**
     * Make a JSON-RPC call to a KBase service
     * @param {string} url - Service URL
     * @param {string} method - Method name
     * @param {Array} params - Method parameters
     * @returns {Promise<any>} Service response
     */
    async makeJsonRpcCall(url, method, params) {
        const requestBody = {
            version: '1.1',
            method: method,
            params: params,
            id: String(Math.random()).slice(2)
        };

        const response = await fetch(url, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'Authorization': this.token
            },
            body: JSON.stringify(requestBody)
        });

        if (!response.ok) {
            throw new Error(`HTTP error! status: ${response.status}`);
        }

        const data = await response.json();
        if (data.error) {
            throw new Error(`JSON-RPC error: ${JSON.stringify(data.error)}`);
        }

        return data.result;
    }

    /**
     * Parse workspace reference into components
     * @param {string} ref - Workspace reference (wsid/objid/ver or wsid/objid)
     * @returns {Object} Parsed reference with wsid, objid, and ver
     */
    parseRef(ref) {
        const parts = ref.split('/');
        return {
            wsid: parseInt(parts[0]),
            objid: parseInt(parts[1]),
            ver: parts[2] ? parseInt(parts[2]) : null
        };
    }

    /**
     * Create workspace reference string
     * @param {number} wsid - Workspace ID
     * @param {number} objid - Object ID
     * @param {number} ver - Version (optional)
     * @returns {string} Workspace reference
     */
    createRef(wsid, objid, ver = null) {
        return ver !== null ? `${wsid}/${objid}/${ver}` : `${wsid}/${objid}`;
    }
}

/**
 * Simple Workspace Client for making workspace API calls
 */
class WorkspaceClient {
    constructor(url, token) {
        this.url = url;
        this.token = token;
    }

    async call(method, params) {
        const requestBody = {
            version: '1.1',
            method: method,
            params: params,
            id: String(Math.random()).slice(2)
        };

        const response = await fetch(this.url, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'Authorization': this.token
            },
            body: JSON.stringify(requestBody)
        });

        if (!response.ok) {
            throw new Error(`HTTP error! status: ${response.status}`);
        }

        const data = await response.json();
        if (data.error) {
            throw new Error(`Workspace error: ${JSON.stringify(data.error)}`);
        }

        return data.result;
    }

    async getObjects(objects) {
        return await this.call('Workspace.get_objects2', [{objects: objects}]);
    }

    async listObjects(params) {
        return await this.call('Workspace.list_objects', [params]);
    }

    async getWorkspaceInfo(params) {
        return await this.call('Workspace.get_workspace_info', [params]);
    }

    async saveObjects(params) {
        return await this.call('Workspace.save_objects', [params]);
    }
}

// Initialize global KBase utilities instance
window.kbReportUtils = new KBaseReportUtils();
"""
        return js_code

    def create_html_report(
        self,
        title: str,
        content: str,
        include_kbase_js: bool = True,
        custom_css: Optional[str] = None,
        custom_js: Optional[str] = None,
        include_bootstrap: bool = True,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Create a complete HTML report document.

        Args:
            title: Report title
            content: HTML content for the report body
            include_kbase_js: Whether to include KBase JavaScript utilities
            custom_css: Additional CSS styles
            custom_js: Additional JavaScript code
            include_bootstrap: Whether to include Bootstrap CSS
            metadata: Additional metadata to embed in the report

        Returns:
            Complete HTML document as a string
        """
        html_parts = ['<!DOCTYPE html>', '<html lang="en">', "<head>"]

        # Add meta tags
        html_parts.append('    <meta charset="UTF-8">')
        html_parts.append(
            '    <meta name="viewport" content="width=device-width, initial-scale=1.0">'
        )
        html_parts.append(f"    <title>{title}</title>")

        # Add Bootstrap if requested
        if include_bootstrap:
            html_parts.append(
                '    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.1.3/dist/css/bootstrap.min.css" '
                'rel="stylesheet">'
            )

        # Add custom CSS
        if custom_css:
            html_parts.append("    <style>")
            html_parts.append(custom_css)
            html_parts.append("    </style>")

        # Add default styling
        html_parts.append("    <style>")
        html_parts.append(
            """
        body {
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
            padding: 20px;
            background-color: #f5f5f5;
        }
        .report-container {
            max-width: 1200px;
            margin: 0 auto;
            background-color: white;
            padding: 30px;
            border-radius: 8px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
        }
        .report-title {
            color: #333;
            border-bottom: 2px solid #4CAF50;
            padding-bottom: 10px;
            margin-bottom: 20px;
        }
        .kbase-badge {
            display: inline-block;
            background-color: #4CAF50;
            color: white;
            padding: 4px 12px;
            border-radius: 4px;
            font-size: 12px;
            margin-left: 10px;
        }
        """
        )
        html_parts.append("    </style>")
        html_parts.append("</head>")
        html_parts.append("<body>")

        # Add report container
        html_parts.append('    <div class="report-container">')
        html_parts.append(f'        <h1 class="report-title">')
        html_parts.append(f"            {title}")
        html_parts.append('            <span class="kbase-badge">KBase Report</span>')
        html_parts.append("        </h1>")

        # Add metadata if provided
        if metadata:
            html_parts.append('        <div id="report-metadata" style="display:none;">')
            html_parts.append(f"            {json.dumps(metadata)}")
            html_parts.append("        </div>")

        # Add main content
        html_parts.append('        <div class="report-content">')
        html_parts.append(content)
        html_parts.append("        </div>")
        html_parts.append("    </div>")

        # Add KBase JavaScript library if requested
        if include_kbase_js:
            html_parts.append("    <script>")
            html_parts.append(self.get_kbase_js_library())
            html_parts.append("    </script>")

        # Add custom JavaScript
        if custom_js:
            html_parts.append("    <script>")
            html_parts.append(custom_js)
            html_parts.append("    </script>")

        # Add Bootstrap JS if included
        if include_bootstrap:
            html_parts.append(
                '    <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.1.3/dist/js/bootstrap.bundle.min.js"></script>'
            )

        html_parts.append("</body>")
        html_parts.append("</html>")

        return "\n".join(html_parts)

    def save_html_report(
        self,
        html_content: str,
        report_name: str,
        workspace_id: Union[int, str],
        report_params: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Save HTML report to KBase workspace.

        Args:
            html_content: Complete HTML document content
            report_name: Name for the report object
            workspace_id: Workspace ID or name where report will be saved
            report_params: Additional report parameters (objects_created, warnings, etc.)

        Returns:
            Report creation response from workspace

        Example:
            >>> html = report_utils.create_html_report("My Report", "<p>Content</p>")
            >>> result = report_utils.save_html_report(
            ...     html_content=html,
            ...     report_name="my_analysis_report",
            ...     workspace_id=12345
            ... )
        """
        # Set workspace
        self.set_ws(workspace_id)

        # Create report object structure
        report_obj = {
            "text_message": report_params.get("text_message", "")
            if report_params
            else "",
            "warnings": report_params.get("warnings", []) if report_params else [],
            "objects_created": report_params.get("objects_created", [])
            if report_params
            else [],
            "direct_html": html_content,
            "direct_html_link_index": 0,
            "html_links": [],
            "file_links": [],
        }

        # Save report object
        save_params = {
            "id": self.ws_id,
            "objects": [
                {
                    "type": "KBaseReport.Report",
                    "data": report_obj,
                    "name": report_name,
                    "meta": {},
                    "provenance": self.get_provenance(),
                }
            ],
        }

        result = self.ws_client().save_objects(save_params)
        report_ref = f"{result[0][6]}/{result[0][0]}/{result[0][4]}"

        return {
            "report_name": report_name,
            "report_ref": report_ref,
            "workspace_id": self.ws_id,
        }

    def create_simple_table_report(
        self,
        title: str,
        table_data: List[Dict[str, Any]],
        columns: Optional[List[str]] = None,
        description: Optional[str] = None,
    ) -> str:
        """Create a simple HTML table report from data.

        Args:
            title: Report title
            table_data: List of dictionaries containing table data
            columns: Column names to display (defaults to all keys in first row)
            description: Optional description text

        Returns:
            HTML content string
        """
        if not table_data:
            return "<p>No data to display</p>"

        # Determine columns
        if columns is None:
            columns = list(table_data[0].keys())

        # Build table HTML
        content_parts = []

        if description:
            content_parts.append(f"<p>{description}</p>")

        content_parts.append('<table class="table table-striped table-bordered">')
        content_parts.append("    <thead>")
        content_parts.append("        <tr>")
        for col in columns:
            content_parts.append(f"            <th>{col}</th>")
        content_parts.append("        </tr>")
        content_parts.append("    </thead>")
        content_parts.append("    <tbody>")

        for row in table_data:
            content_parts.append("        <tr>")
            for col in columns:
                value = row.get(col, "")
                content_parts.append(f"            <td>{value}</td>")
            content_parts.append("        </tr>")

        content_parts.append("    </tbody>")
        content_parts.append("</table>")

        content = "\n".join(content_parts)
        return self.create_html_report(title=title, content=content)

    def create_interactive_report(
        self,
        title: str,
        sections: List[Dict[str, str]],
        workspace_ref: Optional[str] = None,
    ) -> str:
        """Create an interactive HTML report with multiple sections.

        Args:
            title: Report title
            sections: List of section dictionaries with 'title' and 'content' keys
            workspace_ref: Optional workspace reference to embed in report

        Returns:
            HTML content string
        """
        content_parts = []

        # Add navigation tabs if multiple sections
        if len(sections) > 1:
            content_parts.append('<ul class="nav nav-tabs" role="tablist">')
            for i, section in enumerate(sections):
                active = "active" if i == 0 else ""
                content_parts.append(
                    f'    <li class="nav-item" role="presentation">'
                )
                content_parts.append(
                    f'        <button class="nav-link {active}" id="tab-{i}" '
                    f'data-bs-toggle="tab" data-bs-target="#section-{i}" '
                    f'type="button" role="tab">{section["title"]}</button>'
                )
                content_parts.append("    </li>")
            content_parts.append("</ul>")

            # Add tab content
            content_parts.append('<div class="tab-content" style="padding-top: 20px;">')
            for i, section in enumerate(sections):
                active = "show active" if i == 0 else ""
                content_parts.append(
                    f'    <div class="tab-pane fade {active}" id="section-{i}" '
                    f'role="tabpanel">'
                )
                content_parts.append(f"        {section['content']}")
                content_parts.append("    </div>")
            content_parts.append("</div>")
        else:
            # Single section
            content_parts.append(sections[0]["content"])

        content = "\n".join(content_parts)

        # Add workspace reference to metadata if provided
        metadata = {"workspace_ref": workspace_ref} if workspace_ref else None

        return self.create_html_report(
            title=title, content=content, metadata=metadata
        )
