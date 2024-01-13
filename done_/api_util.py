import aiohttp
import asyncio
import hashlib
import json
import logging
import re
from functools import lru_cache
from typing import Any, Callable, Dict, Optional

# Enable basic logging with a uniform format
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Global cache for storing API responses
response_cache = {}

class APIUtil:
    """
    A utility class for assisting with common API usage patterns.
    """

    @staticmethod
    def api_method(http_session: aiohttp.ClientSession, method: str = "post") -> Callable:
        """
        Returns the corresponding HTTP method function from the http_session object.

        Args:
            http_session: The session object from the aiohttp library.
            method: The HTTP method as a string.

        Returns:
            The callable for the specified HTTP method.

        Raises:
            ValueError: If the method is not one of the allowed ones.

        Examples:
            >>> session = aiohttp.ClientSession()
            >>> post_method = APIUtil.api_method(session, "post")
            >>> print(post_method)
            <bound method ClientSession._request of <aiohttp.client.ClientSession object at 0x...>>
        """
        if method not in ["post", "delete", "head", "options", "patch"]:
            raise ValueError("Invalid request, method must be in ['post', 'delete', 'head', 'options', 'patch']")
        return getattr(http_session, method)

    @staticmethod
    def api_error(response_json: Dict[str, Any]) -> bool:
        """
        Checks if the given response_json dictionary contains an "error" key.

        Args:
            response_json: The JSON response as a dictionary.

        Returns:
            True if there is an error, False otherwise.

        Examples:
            >>> response_json_with_error = {"error": "Something went wrong"}
            >>> APIUtil.api_error(response_json_with_error)
            True
            >>> response_json_without_error = {"result": "Success"}
            >>> APIUtil.api_error(response_json_without_error)
            False
        """
        if "error" in response_json:
            logging.warning(f"API call failed with error: {response_json['error']}")
            return True
        return False

    @staticmethod
    def api_rate_limit_error(response_json: Dict[str, Any]) -> bool:
        """
        Checks if the error message in the response_json dictionary contains the phrase "Rate limit".
        
        Args:
            response_json: The JSON response as a dictionary.

        Returns:
            True if the phrase "Rate limit" is found, False otherwise.

        Examples:
            >>> response_json_with_rate_limit = {"error": {"message": "Rate limit exceeded"}}
            >>> api_rate_limit_error(response_json_with_rate_limit)
            True
            >>> response_json_without_rate_limit = {"error": {"message": "Another error"}}
            >>> api_rate_limit_error(response_json_without_rate_limit)
            False
        """
        return "Rate limit" in response_json.get("error", {}).get("message", "")

    @staticmethod
    @lru_cache(maxsize=128)
    def api_endpoint_from_url(request_url: str) -> str:
        """
        Extracts the API endpoint from a given URL using a regular expression.

        Args:
            request_url: The full URL to the API endpoint.

        Returns:
            The extracted endpoint or an empty string if the pattern does not match.

        Examples:
            >>> valid_url = "https://api.example.com/v1/users"
            >>> api_endpoint_from_url(valid_url)
            'users'
            >>> invalid_url = "https://api.example.com/users"
            >>> api_endpoint_from_url(invalid_url)
            ''
        """
        match = re.search(r"^https://[^/]+/v\d+/(.+)$", request_url)
        return match.group(1) if match else ""

    @staticmethod
    async def unified_api_call(http_session: aiohttp.ClientSession, method: str, url: str, **kwargs) -> Any:
        """
        Makes an API call and automatically retries on rate limit error.

        Args:
            http_session: The session object from the aiohttp library.
            method: The HTTP method as a string.
            url: The URL to which the request is made.
            **kwargs: Additional keyword arguments to pass to the API call.

        Returns:
            The JSON response as a dictionary.

        Examples:
            >>> session = aiohttp.ClientSession()
            >>> success_url = "https://api.example.com/v1/success"
            >>> print(await unified_api_call(session, 'get', success_url))
            {'result': 'Success'}
            >>> rate_limit_url = "https://api.example.com/v1/rate_limit"
            >>> print(await unified_api_call(session, 'get', rate_limit_url))
            {'error': {'message': 'Rate limit exceeded'}}
        """
        api_call = APIUtil.api_method(http_session, method)
        retry_count = 3
        retry_delay = 5  # seconds

        for attempt in range(retry_count):
            async with api_call(url, **kwargs) as response:
                response_json = await response.json()

                if not APIUtil.api_error(response_json):
                    return response_json

                if APIUtil.api_rate_limit_error(response_json) and attempt < retry_count - 1:
                    logging.warning(f"Rate limit error detected. Retrying in {retry_delay} seconds...")
                    await asyncio.sleep(retry_delay)
                else:
                    break

        return response_json

    @staticmethod
    def get_cache_key(url: str, params: Optional[Dict[str, Any]]) -> str:
        """
        Creates a unique cache key based on the URL and parameters.
        (Other documentation remains unchanged)
        """
        param_str = json.dumps(params, sort_keys=True) if params else ""
        return hashlib.md5((url + param_str).encode('utf-8')).hexdigest()

    @staticmethod
    async def retry_api_call(http_session: aiohttp.ClientSession, url: str, retries: int = 3, backoff_factor: float = 0.5, **kwargs) -> Any:
        """
        Retries an API call on failure, with exponential backoff.

        Args:
            http_session: The aiohttp client session.
            url: The URL to make the API call.
            retries: The number of times to retry.
            backoff_factor: The backoff factor for retries.
            **kwargs: Additional arguments for the API call.

        Returns:
            The response from the API call, if successful; otherwise, None.
        """
        for attempt in range(retries):
            try:
                async with http_session.get(url, **kwargs) as response:
                    response.raise_for_status()
                    return await response.json()
            except aiohttp.ClientError:
                if attempt < retries - 1:
                    delay = backoff_factor * (2 ** attempt)
                    logging.info(f"Retrying {url} in {delay} seconds...")
                    await asyncio.sleep(delay)
                else:
                    logging.error(f"Failed to retrieve data from {url} after {retries} attempts.")
                    return None

    @staticmethod
    async def upload_file_with_retry(http_session: aiohttp.ClientSession, url: str, file_path: str, param_name: str = 'file', additional_data: Dict[str, Any] = None, retries: int = 3) -> Any:
        """
        Uploads a file to a specified URL with a retry mechanism for handling failures.

        Args:
            http_session: The HTTP session object to use for making the request.
            url: The URL to which the file will be uploaded.
            file_path: The path to the file that will be uploaded.
            param_name: The name of the parameter expected by the server for the file upload.
            additional_data: Additional data to be sent with the upload.
            retries: The number of times to retry the upload in case of failure.

        Returns:
            The HTTP response object.

        Examples:
            >>> session = aiohttp.ClientSession()
            >>> response = await APIUtil.upload_file_with_retry(session, 'http://example.com/upload', 'path/to/file.txt')
            >>> response.status
            200
        """
        for attempt in range(retries):
            try:
                with open(file_path, 'rb') as file:
                    files = {param_name: file}
                    async with http_session.post(url, data=additional_data, files=files) as response:
                        response.raise_for_status()
                        return await response.json()
            except aiohttp.ClientError as e:
                if attempt == retries - 1:
                    raise e
                backoff = 2 ** attempt
                logging.info(f"Retrying {url} in {backoff} seconds...")
                await asyncio.sleep(backoff)

    @staticmethod
    @lru_cache(maxsize=128)
    async def get_oauth_token_with_cache(http_session: aiohttp.ClientSession, auth_url: str, client_id: str, client_secret: str, scope: str) -> str:
        """
        Retrieves an OAuth token from the authentication server and caches it to avoid unnecessary requests.

        Args:
            http_session: The HTTP session object to use for making the request.
            auth_url: The URL of the authentication server.
            client_id: The client ID for OAuth authentication.
            client_secret: The client secret for OAuth authentication.
            scope: The scope for which the OAuth token is requested.

        Returns:
            The OAuth token as a string.

        Examples:
            >>> session = aiohttp.ClientSession()
            >>> token = await APIUtil.get_oauth_token_with_cache(session, 'http://auth.example.com', 'client_id', 'client_secret', 'read')
            >>> token
            'mock_access_token'
        """
        async with http_session.post(auth_url, data={
            'grant_type': 'client_credentials',
            'client_id': client_id,
            'client_secret': client_secret,
            'scope': scope
        }) as auth_response:
            auth_response.raise_for_status()
            return (await auth_response.json()).get('access_token')
        
    @staticmethod
    async def cached_api_call(http_session: aiohttp.ClientSession, url: str, **kwargs) -> Any:
        """
        Makes an API call.

        Args:
            http_session: The aiohttp client session.
            url: The URL for the API call.
            **kwargs: Additional arguments for the API call.

        Returns:
            The response from the API call, if successful; otherwise, None.
        """
        try:
            async with http_session.get(url, **kwargs) as response:
                response.raise_for_status()
                return await response.json()
        except aiohttp.ClientError as e:
            logging.error(f"API call to {url} failed: {e}")
            return None