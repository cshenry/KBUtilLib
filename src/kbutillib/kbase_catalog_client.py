"""
KBase Catalog Client for Programmatic Module Registration

This module provides a Python client for interacting with the KBase Catalog service
to register, update, and manage KBase SDK modules programmatically.

Usage:
    from kbase_catalog_client import CatalogClient

    # Initialize with your KBase token
    client = CatalogClient(token="your_kbase_token")

    # Register a module
    result = client.register_repo(
        git_url="https://github.com/kbaseapps/KBDatalakeApps"
    )

    # Check registration status
    info = client.get_module_info(module_name="KBDatalakeApps")

    # Push dev to beta
    client.push_dev_to_beta(module_name="KBDatalakeApps")

    # Request release
    client.request_release(module_name="KBDatalakeApps")
"""

import json
import os
import requests
import random
import time
from typing import Optional, Dict, List, Any


class CatalogError(Exception):
    """Exception raised for Catalog service errors."""

    def __init__(self, name: str, code: int, message: str, data: str = ""):
        super().__init__(message)
        self.name = name
        self.code = code
        self.message = message
        self.data = data

    def __str__(self):
        return f"{self.name}: {self.code}. {self.message}\n{self.data}"


class CatalogClient:
    """
    Client for the KBase Catalog service.

    The Catalog service manages module registration, builds, and releases
    for KBase SDK applications.

    Attributes:
        url: The Catalog service URL
        token: KBase authentication token
    """

    # Default URLs for different KBase environments
    ENVIRONMENTS = {
        "prod": "https://kbase.us/services/catalog",
        "appdev": "https://appdev.kbase.us/services/catalog",
        "ci": "https://ci.kbase.us/services/catalog",
        "next": "https://next.kbase.us/services/catalog",
    }

    def __init__(
        self,
        url: Optional[str] = None,
        token: Optional[str] = None,
        environment: str = "appdev",
        timeout: int = 1800
    ):
        """
        Initialize the Catalog client.

        Args:
            url: Direct URL to the Catalog service. If not provided, uses environment.
            token: KBase authentication token. If not provided, reads from KB_AUTH_TOKEN.
            environment: KBase environment (prod, appdev, ci, next). Default: appdev.
            timeout: Request timeout in seconds. Default: 1800.
        """
        if url:
            self.url = url
        else:
            self.url = self.ENVIRONMENTS.get(environment, self.ENVIRONMENTS["appdev"])

        self.timeout = timeout
        self._headers = {"Content-Type": "application/json"}

        # Get token from parameter, environment variable, or config file
        if token:
            self._headers["Authorization"] = token
        elif "KB_AUTH_TOKEN" in os.environ:
            self._headers["Authorization"] = os.environ["KB_AUTH_TOKEN"]
        else:
            # Try to read from ~/.kbase_config
            config_path = os.path.expanduser("~/.kbase_config")
            if os.path.exists(config_path):
                import configparser
                config = configparser.ConfigParser()
                config.read(config_path)
                if config.has_option("authentication", "token"):
                    self._headers["Authorization"] = config.get("authentication", "token")

    def _call(self, method: str, params: List[Any]) -> Any:
        """
        Make a JSON-RPC call to the Catalog service.

        Args:
            method: The method name (e.g., "Catalog.register_repo")
            params: List of parameters for the method

        Returns:
            The result from the service

        Raises:
            CatalogError: If the service returns an error
        """
        payload = {
            "method": method,
            "params": params,
            "version": "1.1",
            "id": str(random.random())[2:]
        }

        response = requests.post(
            self.url,
            data=json.dumps(payload),
            headers=self._headers,
            timeout=self.timeout
        )

        if response.status_code == 500:
            if response.headers.get("content-type") == "application/json":
                err = response.json()
                if "error" in err:
                    raise CatalogError(**err["error"])
            raise CatalogError("Unknown", 0, response.text)

        response.raise_for_status()
        result = response.json()

        if "result" not in result:
            raise CatalogError("Unknown", 0, "An unknown server error occurred")

        if not result["result"]:
            return None

        if len(result["result"]) == 1:
            return result["result"][0]

        return result["result"]

    # -------------------------------------------------------------------------
    # Module Registration Methods
    # -------------------------------------------------------------------------

    def register_repo(
        self,
        git_url: str,
        git_commit_hash: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Register a module repository with the Catalog.

        This creates an initial registration or updates an existing dev version.
        The Catalog will trigger a build of the module's Docker image.

        Args:
            git_url: Public GitHub URL of the module repository
            git_commit_hash: Optional specific commit to build. If not provided,
                            the latest commit on the default branch is used.

        Returns:
            Registration result with registration_id for tracking the build

        Example:
            result = client.register_repo(
                git_url="https://github.com/kbaseapps/KBDatalakeApps"
            )
            print(f"Registration ID: {result['registration_id']}")
        """
        params = {"git_url": git_url}
        if git_commit_hash:
            params["git_commit_hash"] = git_commit_hash

        return self._call("Catalog.register_repo", [params])

    def push_dev_to_beta(
        self,
        module_name: Optional[str] = None,
        git_url: Optional[str] = None
    ) -> None:
        """
        Push the current dev version to beta.

        This immediately updates the beta tag to what is currently in dev.

        Args:
            module_name: Name of the module (e.g., "KBDatalakeApps")
            git_url: Git URL of the module (alternative to module_name)

        Note:
            You must provide either module_name or git_url.
        """
        params = {}
        if module_name:
            params["module_name"] = module_name
        if git_url:
            params["git_url"] = git_url

        return self._call("Catalog.push_dev_to_beta", [params])

    def request_release(
        self,
        module_name: Optional[str] = None,
        git_url: Optional[str] = None
    ) -> None:
        """
        Request release of a module from beta to production.

        This creates a release request that must be approved by a KBase admin.

        Args:
            module_name: Name of the module
            git_url: Git URL of the module (alternative to module_name)
        """
        params = {}
        if module_name:
            params["module_name"] = module_name
        if git_url:
            params["git_url"] = git_url

        return self._call("Catalog.request_release", [params])

    # -------------------------------------------------------------------------
    # Module Information Methods
    # -------------------------------------------------------------------------

    def get_module_info(
        self,
        module_name: Optional[str] = None,
        git_url: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Get detailed information about a module.

        Args:
            module_name: Name of the module
            git_url: Git URL of the module

        Returns:
            Module information including:
            - module_name: Name of the module
            - git_url: Repository URL
            - description: Module description
            - language: Programming language
            - owners: List of owner usernames
            - release: Current release version info
            - beta: Current beta version info
            - dev: Current dev version info
        """
        params = {}
        if module_name:
            params["module_name"] = module_name
        if git_url:
            params["git_url"] = git_url

        return self._call("Catalog.get_module_info", [params])

    def get_module_state(
        self,
        module_name: Optional[str] = None,
        git_url: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Get the current state of a module.

        Args:
            module_name: Name of the module
            git_url: Git URL of the module

        Returns:
            Module state including registration status and approval state
        """
        params = {}
        if module_name:
            params["module_name"] = module_name
        if git_url:
            params["git_url"] = git_url

        return self._call("Catalog.get_module_state", [params])

    def get_module_version(
        self,
        module_name: Optional[str] = None,
        git_url: Optional[str] = None,
        version: Optional[str] = None,
        git_commit_hash: Optional[str] = None,
        include_module_description: bool = False,
        include_compilation_report: bool = False
    ) -> Dict[str, Any]:
        """
        Get information about a specific version of a module.

        Args:
            module_name: Name of the module
            git_url: Git URL of the module
            version: Version tag (dev, beta, release, or git hash)
            git_commit_hash: Specific git commit
            include_module_description: Include full description
            include_compilation_report: Include compilation details

        Returns:
            Version information
        """
        params = {}
        if module_name:
            params["module_name"] = module_name
        if git_url:
            params["git_url"] = git_url
        if version:
            params["version"] = version
        if git_commit_hash:
            params["git_commit_hash"] = git_commit_hash
        if include_module_description:
            params["include_module_description"] = 1
        if include_compilation_report:
            params["include_compilation_report"] = 1

        return self._call("Catalog.get_module_version", [params])

    def list_basic_module_info(
        self,
        owners: Optional[List[str]] = None,
        include_disabled: bool = False,
        include_released: bool = True,
        include_unreleased: bool = True
    ) -> List[Dict[str, Any]]:
        """
        List basic information about modules.

        Args:
            owners: Filter by owner usernames
            include_disabled: Include disabled modules
            include_released: Include released modules
            include_unreleased: Include unreleased modules

        Returns:
            List of module information dictionaries
        """
        params = {}
        if owners:
            params["owners"] = owners
        if include_disabled:
            params["include_disabled"] = 1
        params["include_released"] = 1 if include_released else 0
        params["include_unreleased"] = 1 if include_unreleased else 0

        return self._call("Catalog.list_basic_module_info", [params])

    # -------------------------------------------------------------------------
    # Build Information Methods
    # -------------------------------------------------------------------------

    def get_build_log(self, registration_id: str) -> str:
        """
        Get the build log for a registration.

        Args:
            registration_id: The registration ID from register_repo

        Returns:
            The complete build log as a string
        """
        return self._call("Catalog.get_build_log", [registration_id])

    def list_builds(
        self,
        module_name: Optional[str] = None,
        git_url: Optional[str] = None,
        limit: int = 10,
        skip: int = 0
    ) -> List[Dict[str, Any]]:
        """
        List builds for a module.

        Args:
            module_name: Name of the module
            git_url: Git URL of the module
            limit: Maximum number of builds to return
            skip: Number of builds to skip

        Returns:
            List of build information dictionaries
        """
        params = {"limit": limit, "skip": skip}
        if module_name:
            params["module_name"] = module_name
        if git_url:
            params["git_url"] = git_url

        return self._call("Catalog.list_builds", [params])

    def is_registered(
        self,
        module_name: Optional[str] = None,
        git_url: Optional[str] = None
    ) -> Dict[str, bool]:
        """
        Check if a module is registered.

        Args:
            module_name: Name of the module
            git_url: Git URL of the module

        Returns:
            Dictionary with 'is_registered' boolean
        """
        params = {}
        if module_name:
            params["module_name"] = module_name
        if git_url:
            params["git_url"] = git_url

        return self._call("Catalog.is_registered", [params])

    # -------------------------------------------------------------------------
    # Convenience Methods
    # -------------------------------------------------------------------------

    def wait_for_build(
        self,
        registration_id: str,
        timeout: int = 3600,
        poll_interval: int = 30,
        verbose: bool = True
    ) -> Dict[str, Any]:
        """
        Wait for a build to complete.

        Args:
            registration_id: The registration ID from register_repo
            timeout: Maximum time to wait in seconds
            poll_interval: Time between status checks in seconds
            verbose: Print status updates

        Returns:
            Final build status

        Raises:
            TimeoutError: If build doesn't complete within timeout
            CatalogError: If build fails
        """
        start_time = time.time()

        while time.time() - start_time < timeout:
            try:
                log = self.get_build_log(registration_id)

                if verbose:
                    # Print last few lines of log
                    lines = log.strip().split("\n")
                    print(f"[{time.strftime('%H:%M:%S')}] {lines[-1] if lines else 'Building...'}")

                # Check if build completed (look for success/failure markers)
                if "Build completed successfully" in log or "successfully built" in log.lower():
                    return {"status": "success", "registration_id": registration_id}
                elif "Build failed" in log or "error" in log.lower()[-500:]:
                    # Check last 500 chars for recent errors
                    if "Build failed" in log:
                        raise CatalogError("BuildFailed", 1, "Build failed", log[-1000:])

            except CatalogError as e:
                if "not found" not in str(e).lower():
                    raise

            time.sleep(poll_interval)

        raise TimeoutError(f"Build did not complete within {timeout} seconds")

    def register_and_wait(
        self,
        git_url: str,
        git_commit_hash: Optional[str] = None,
        timeout: int = 3600,
        verbose: bool = True
    ) -> Dict[str, Any]:
        """
        Register a module and wait for the build to complete.

        Args:
            git_url: Public GitHub URL of the module repository
            git_commit_hash: Optional specific commit to build
            timeout: Maximum time to wait for build
            verbose: Print status updates

        Returns:
            Registration result with build status

        Example:
            result = client.register_and_wait(
                git_url="https://github.com/kbaseapps/KBDatalakeApps",
                verbose=True
            )
        """
        if verbose:
            print(f"Registering module from {git_url}...")

        result = self.register_repo(git_url, git_commit_hash)
        registration_id = result.get("registration_id")

        if verbose:
            print(f"Registration started with ID: {registration_id}")
            print("Waiting for build to complete...")

        build_result = self.wait_for_build(registration_id, timeout, verbose=verbose)

        return {
            "registration_id": registration_id,
            "build_status": build_result["status"],
            "git_url": git_url
        }


# Convenience function for quick registration
def register_module(
    git_url: str,
    token: Optional[str] = None,
    environment: str = "appdev",
    wait: bool = True,
    verbose: bool = True
) -> Dict[str, Any]:
    """
    Convenience function to register a KBase module.

    Args:
        git_url: GitHub URL of the module
        token: KBase auth token (uses KB_AUTH_TOKEN if not provided)
        environment: KBase environment (appdev, prod, ci, next)
        wait: Wait for build to complete
        verbose: Print status messages

    Returns:
        Registration result

    Example:
        from kbase_catalog_client import register_module

        result = register_module(
            git_url="https://github.com/kbaseapps/KBDatalakeApps",
            environment="appdev"
        )
    """
    client = CatalogClient(token=token, environment=environment)

    if wait:
        return client.register_and_wait(git_url, verbose=verbose)
    else:
        return client.register_repo(git_url)


if __name__ == "__main__":
    # Example usage
    import argparse

    parser = argparse.ArgumentParser(description="Register a KBase module")
    parser.add_argument("--git-url", required=True, help="GitHub URL of the module")
    parser.add_argument("--token", help="KBase auth token (or set KB_AUTH_TOKEN)")
    parser.add_argument("--env", default="appdev", choices=["appdev", "prod", "ci", "next"],
                       help="KBase environment")
    parser.add_argument("--no-wait", action="store_true", help="Don't wait for build")
    parser.add_argument("--push-beta", action="store_true", help="Push dev to beta after build")
    parser.add_argument("--info", action="store_true", help="Just show module info")

    args = parser.parse_args()

    client = CatalogClient(token=args.token, environment=args.env)

    if args.info:
        # Just show module info
        info = client.get_module_info(git_url=args.git_url)
        print(json.dumps(info, indent=2))
    else:
        # Register the module
        if args.no_wait:
            result = client.register_repo(args.git_url)
            print(f"Registration started: {result}")
        else:
            result = client.register_and_wait(args.git_url, verbose=True)
            print(f"\nRegistration complete: {result}")

        # Optionally push to beta
        if args.push_beta and result.get("build_status") == "success":
            print("\nPushing dev to beta...")
            client.push_dev_to_beta(git_url=args.git_url)
            print("Beta push complete!")
