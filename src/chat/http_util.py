# http_util.py — small async HTTP helper shared by the chat bots

import json

import aiohttp

BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


async def request(method, url, *, headers=None, data=None, json_body=None,
                  timeout=20, impersonate=False, basic_auth=None):
    """Returns (status, text). Status 0 means the request never got an answer.

    impersonate=True goes through curl_cffi looking like Chrome when it's
    installed — kick.com sits behind Cloudflare, which turns away plain
    Python clients more often than not.
    """
    if impersonate:
        try:
            from curl_cffi.requests import AsyncSession
        except Exception:
            AsyncSession = None
        if AsyncSession is not None:
            try:
                async with AsyncSession(impersonate="chrome") as s:
                    r = await s.request(method, url, headers=headers, data=data,
                                        json=json_body, timeout=timeout,
                                        auth=basic_auth)
                    return r.status_code, r.text
            except Exception:
                pass            # fall back to aiohttp below
    try:
        auth = aiohttp.BasicAuth(*basic_auth) if basic_auth else None
        async with aiohttp.ClientSession(trust_env=True) as s:
            async with s.request(method, url, headers=headers, data=data, json=json_body,
                                 auth=auth,
                                 timeout=aiohttp.ClientTimeout(total=timeout)) as r:
                return r.status, await r.text()
    except Exception as e:
        return 0, f"{type(e).__name__}: {e}"


def loads(text):
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return None
