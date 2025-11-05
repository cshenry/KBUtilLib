/**
 * KBase Report Utilities - JavaScript Library
 *
 * Provides utilities for interacting with KBase services from HTML reports.
 * This library can be used standalone or embedded within HTML reports generated
 * by the Python KBReportUtils class.
 *
 * Features:
 * - Authentication via KBase session cookies
 * - Workspace service calls
 * - Dynamic service calls via Service Wizard
 * - Workspace reference parsing and management
 *
 * Example usage:
 * ```javascript
 * // Initialize the library
 * const kbUtils = new KBaseReportUtils();
 *
 * // Check authentication
 * if (kbUtils.isAuthenticated()) {
 *     // Get a workspace object
 *     const data = await kbUtils.getObject('12345/1/1');
 *     console.log(data);
 *
 *     // Call a dynamic service
 *     const result = await kbUtils.callService(
 *         'MyService',
 *         'MyService.my_method',
 *         [{ param1: 'value' }]
 *     );
 * }
 * ```
 *
 * @author KBase
 * @version 1.0.0
 */

class KBaseReportUtils {
    /**
     * Create a new KBaseReportUtils instance
     * @param {Object} config - Configuration object
     * @param {string} config.workspaceUrl - Workspace service URL (default: production)
     * @param {string} config.serviceWizardUrl - Service Wizard URL (default: production)
     * @param {string} config.narrativeUrl - Narrative service URL (default: production)
     * @param {string} config.environment - KBase environment ('prod', 'appdev', 'ci')
     */
    constructor(config = {}) {
        // Determine environment
        const env = config.environment || this.detectEnvironment();

        // Set default URLs based on environment
        const baseUrls = {
            'prod': 'https://kbase.us/services',
            'appdev': 'https://appdev.kbase.us/services',
            'ci': 'https://ci.kbase.us/services'
        };

        const baseUrl = baseUrls[env] || baseUrls['prod'];

        this.config = {
            workspaceUrl: config.workspaceUrl || `${baseUrl}/ws`,
            serviceWizardUrl: config.serviceWizardUrl || `${baseUrl}/service_wizard`,
            narrativeUrl: config.narrativeUrl || 'https://narrative.kbase.us',
            environment: env,
            ...config
        };

        this.token = this.getKBaseToken();
        this.wsClient = null;
        this.serviceUrlCache = {};

        if (this.token) {
            this.initializeClients();
        }
    }

    /**
     * Detect KBase environment from current URL
     * @returns {string} Environment name ('prod', 'appdev', 'ci')
     */
    detectEnvironment() {
        const hostname = window.location.hostname;
        if (hostname.includes('appdev')) return 'appdev';
        if (hostname.includes('ci')) return 'ci';
        return 'prod';
    }

    /**
     * Get KBase authentication token from browser cookies
     * Checks both kbase_session and kbase_session_backup cookies
     * @returns {string|null} KBase session token or null if not found
     */
    getKBaseToken() {
        try {
            const cookies = Object.fromEntries(
                document.cookie.split('; ')
                    .filter(cookie => cookie.length > 0)
                    .map((cookie) => {
                        const parts = cookie.split('=');
                        return [parts[0], parts.slice(1).join('=')];
                    })
            );
            return cookies.kbase_session || cookies.kbase_session_backup || null;
        } catch (error) {
            console.error('Error reading KBase token from cookies:', error);
            return null;
        }
    }

    /**
     * Initialize workspace and service clients
     */
    initializeClients() {
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
     * Require authentication, throw error if not authenticated
     * @throws {Error} If not authenticated
     */
    requireAuth() {
        if (!this.isAuthenticated()) {
            throw new Error('Not authenticated. KBase token not found in cookies. ' +
                'Please log in to KBase and reload this report.');
        }
    }

    /**
     * Get workspace object by reference
     * @param {string} ref - Workspace reference (wsid/objid/ver)
     * @param {boolean} includeMetadata - Include object metadata
     * @returns {Promise<Object>} Workspace object data
     */
    async getObject(ref, includeMetadata = false) {
        this.requireAuth();
        const result = await this.wsClient.getObjects([{
            ref: ref,
            included: includeMetadata ? ['metadata'] : undefined
        }]);
        return result.data[0];
    }

    /**
     * Get multiple workspace objects
     * @param {Array<string|Object>} objects - Array of refs or object specifications
     * @returns {Promise<Array>} Array of workspace objects
     */
    async getObjects(objects) {
        this.requireAuth();
        const objectSpecs = objects.map(obj =>
            typeof obj === 'string' ? {ref: obj} : obj
        );
        const result = await this.wsClient.getObjects(objectSpecs);
        return result.data;
    }

    /**
     * Get object information without retrieving data
     * @param {string} ref - Workspace reference
     * @returns {Promise<Array>} Object info array
     */
    async getObjectInfo(ref) {
        this.requireAuth();
        return await this.wsClient.getObjectInfo([{ref: ref}]);
    }

    /**
     * List objects in a workspace
     * @param {number|string} workspace - Workspace ID or name
     * @param {Object} options - Additional options
     * @param {string} options.type - Filter by object type
     * @param {boolean} options.includeMetadata - Include metadata (default: false)
     * @param {number} options.limit - Maximum number of objects to return
     * @returns {Promise<Array>} List of object info
     */
    async listObjects(workspace, options = {}) {
        this.requireAuth();
        const params = {
            includeMetadata: options.includeMetadata ? 1 : 0
        };

        if (options.type) {
            params.type = options.type;
        }

        if (options.limit) {
            params.limit = options.limit;
        }

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
     * @returns {Promise<Array>} Workspace info array
     */
    async getWorkspaceInfo(workspace) {
        this.requireAuth();
        const params = typeof workspace === 'number'
            ? {id: workspace}
            : {workspace: workspace};
        return await this.wsClient.getWorkspaceInfo(params);
    }

    /**
     * Save objects to workspace (if authorized)
     * @param {number|string} workspace - Workspace ID or name
     * @param {Array<Object>} objects - Objects to save
     * @returns {Promise<Array>} Save results
     */
    async saveObjects(workspace, objects) {
        this.requireAuth();
        const wsInfo = await this.getWorkspaceInfo(workspace);
        const wsId = wsInfo[0];

        return await this.wsClient.saveObjects({
            id: wsId,
            objects: objects
        });
    }

    /**
     * Call a dynamic service method
     * @param {string} serviceName - Name of the service module
     * @param {string} method - Full method name (ServiceName.method_name)
     * @param {Array} params - Method parameters
     * @param {string} version - Service version (optional, defaults to 'release')
     * @returns {Promise<any>} Service response
     */
    async callService(serviceName, method, params, version = null) {
        this.requireAuth();
        const serviceUrl = await this.getServiceUrl(serviceName, version);
        return await this.makeJsonRpcCall(serviceUrl, method, params);
    }

    /**
     * Get service URL from service wizard
     * @param {string} serviceName - Name of the service
     * @param {string} version - Service version (null for latest release)
     * @returns {Promise<string>} Service URL
     */
    async getServiceUrl(serviceName, version = null) {
        const cacheKey = `${serviceName}:${version || 'release'}`;

        // Return cached URL if available
        if (this.serviceUrlCache[cacheKey]) {
            return this.serviceUrlCache[cacheKey];
        }

        const response = await this.makeJsonRpcCall(
            this.config.serviceWizardUrl,
            'ServiceWizard.get_service_status',
            [{module_name: serviceName, version: version}]
        );

        const url = response[0].url;
        this.serviceUrlCache[cacheKey] = url;
        return url;
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

        const headers = {
            'Content-Type': 'application/json'
        };

        if (this.token) {
            headers['Authorization'] = this.token;
        }

        const response = await fetch(url, {
            method: 'POST',
            headers: headers,
            body: JSON.stringify(requestBody)
        });

        if (!response.ok) {
            throw new Error(`HTTP error! status: ${response.status}`);
        }

        const data = await response.json();
        if (data.error) {
            const error = new Error(`JSON-RPC error: ${data.error.message || JSON.stringify(data.error)}`);
            error.code = data.error.code;
            error.data = data.error.data;
            throw error;
        }

        return data.result;
    }

    /**
     * Parse workspace reference into components
     * @param {string} ref - Workspace reference (wsid/objid/ver or wsid/objid)
     * @returns {Object} Parsed reference with wsid, objid, and ver properties
     */
    parseRef(ref) {
        const parts = ref.split('/');
        if (parts.length < 2) {
            throw new Error(`Invalid workspace reference: ${ref}`);
        }
        return {
            wsid: parseInt(parts[0]),
            objid: parseInt(parts[1]),
            ver: parts[2] ? parseInt(parts[2]) : null,
            toString: function() {
                return this.ver !== null
                    ? `${this.wsid}/${this.objid}/${this.ver}`
                    : `${this.wsid}/${this.objid}`;
            }
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

    /**
     * Get narrative URL for a workspace object
     * @param {string} ref - Workspace reference
     * @returns {string} Narrative data viewer URL
     */
    getObjectUrl(ref) {
        return `${this.config.narrativeUrl}/legacy/dataview/${ref}`;
    }

    /**
     * Get workspace metadata from URL or localStorage
     * @returns {Object} Workspace metadata (id, ref)
     */
    getWorkspaceMetadata() {
        const metadata = {};

        // Try to get from localStorage (development mode)
        try {
            const wsid = localStorage.getItem('wsid');
            if (wsid) {
                metadata.id = parseInt(wsid);
            }
        } catch (e) {
            console.warn('Could not access localStorage:', e);
        }

        // Try to parse from URL path
        const pathMatch = window.location.pathname.match(/\/(\d+)\/(\d+)/);
        if (pathMatch) {
            metadata.id = parseInt(pathMatch[1]);
            metadata.objid = parseInt(pathMatch[2]);
            metadata.ref = `${pathMatch[1]}/${pathMatch[2]}`;
        }

        return metadata;
    }

    /**
     * Display an error message to the user
     * @param {string} message - Error message
     * @param {Error} error - Original error object (optional)
     */
    displayError(message, error = null) {
        console.error(message, error);

        const errorDiv = document.createElement('div');
        errorDiv.style.cssText = `
            background-color: #f8d7da;
            border: 1px solid #f5c6cb;
            color: #721c24;
            padding: 15px;
            margin: 20px;
            border-radius: 4px;
        `;
        errorDiv.innerHTML = `
            <strong>Error:</strong> ${message}
            ${error ? `<br><small>${error.message}</small>` : ''}
        `;

        document.body.insertBefore(errorDiv, document.body.firstChild);
    }

    /**
     * Display a loading indicator
     * @param {string} message - Loading message
     * @returns {HTMLElement} Loading element (call remove() to hide)
     */
    displayLoading(message = 'Loading...') {
        const loadingDiv = document.createElement('div');
        loadingDiv.style.cssText = `
            background-color: #d1ecf1;
            border: 1px solid #bee5eb;
            color: #0c5460;
            padding: 15px;
            margin: 20px;
            border-radius: 4px;
            display: flex;
            align-items: center;
        `;
        loadingDiv.innerHTML = `
            <div style="
                border: 3px solid #f3f3f3;
                border-top: 3px solid #0c5460;
                border-radius: 50%;
                width: 20px;
                height: 20px;
                animation: spin 1s linear infinite;
                margin-right: 10px;
            "></div>
            <span>${message}</span>
            <style>
                @keyframes spin {
                    0% { transform: rotate(0deg); }
                    100% { transform: rotate(360deg); }
                }
            </style>
        `;

        document.body.insertBefore(loadingDiv, document.body.firstChild);
        return loadingDiv;
    }
}

/**
 * Simple Workspace Client for making workspace API calls
 * This is a lightweight client that wraps the Workspace JSON-RPC API
 */
class WorkspaceClient {
    /**
     * Create a new WorkspaceClient
     * @param {string} url - Workspace service URL
     * @param {string} token - KBase authentication token
     */
    constructor(url, token) {
        this.url = url;
        this.token = token;
    }

    /**
     * Make a workspace API call
     * @param {string} method - Workspace method name
     * @param {Array} params - Method parameters
     * @returns {Promise<any>} API response
     */
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
            const error = new Error(
                `Workspace error: ${data.error.message || JSON.stringify(data.error)}`
            );
            error.code = data.error.code;
            error.data = data.error.data;
            throw error;
        }

        return data.result;
    }

    /**
     * Get objects from workspace
     * @param {Array<Object>} objects - Object specifications
     * @returns {Promise<Object>} Objects response
     */
    async getObjects(objects) {
        const result = await this.call('Workspace.get_objects2', [{objects: objects}]);
        return result[0];
    }

    /**
     * Get object information
     * @param {Array<Object>} objects - Object specifications
     * @returns {Promise<Array>} Object info arrays
     */
    async getObjectInfo(objects) {
        return await this.call('Workspace.get_object_info3', [{objects: objects}]);
    }

    /**
     * List objects in workspace
     * @param {Object} params - List parameters
     * @returns {Promise<Array>} List of object info
     */
    async listObjects(params) {
        return await this.call('Workspace.list_objects', [params]);
    }

    /**
     * Get workspace information
     * @param {Object} params - Workspace specification
     * @returns {Promise<Array>} Workspace info array
     */
    async getWorkspaceInfo(params) {
        return await this.call('Workspace.get_workspace_info', [params]);
    }

    /**
     * Save objects to workspace
     * @param {Object} params - Save parameters
     * @returns {Promise<Array>} Save results
     */
    async saveObjects(params) {
        return await this.call('Workspace.save_objects', [params]);
    }
}

// Auto-initialize global instance if in browser environment
if (typeof window !== 'undefined') {
    window.KBaseReportUtils = KBaseReportUtils;
    window.WorkspaceClient = WorkspaceClient;

    // Create a global instance for convenience
    window.kbReportUtils = new KBaseReportUtils();

    console.log('KBase Report Utils loaded. Access via window.kbReportUtils');
}

// Export for Node.js/module environments
if (typeof module !== 'undefined' && module.exports) {
    module.exports = { KBaseReportUtils, WorkspaceClient };
}
