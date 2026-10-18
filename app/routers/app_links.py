"""What a phone and a browser need from the backend's domain for emailed links:

* ``/.well-known/assetlinks.json`` (Android App Links) and
  ``/.well-known/apple-app-site-association`` (iOS Universal Links): public files that
  prove links on this domain belong to the mobile app. Served only once configured.
* ``/verify-email`` and ``/reset-password``: web pages that finish the action in the
  browser when the app isn't installed (or the phone doesn't hand the link to it).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import HTMLResponse, JSONResponse

from ..config import Settings
from ..deps import get_settings_dep
from ..web_pages import new_nonce, reset_password_page, verify_email_page

router = APIRouter(include_in_schema=False)

# Paths the app and the web pages answer. Keep in sync with the links in app/routers/auth.py.
APP_LINK_PATHS = ("/verify-email", "/reset-password")
_WELL_KNOWN_CACHE = {"Cache-Control": "public, max-age=300"}


@router.get("/.well-known/assetlinks.json")
async def assetlinks(settings: Settings = Depends(get_settings_dep)) -> JSONResponse:
    if not settings.android_package_name:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Android app links are not configured")
    return JSONResponse(
        [
            {
                "relation": ["delegate_permission/common.handle_all_urls"],
                "target": {
                    "namespace": "android_app",
                    "package_name": settings.android_package_name,
                    "sha256_cert_fingerprints": settings.android_fingerprint_list,
                },
            }
        ],
        headers=_WELL_KNOWN_CACHE,
    )


@router.get("/.well-known/apple-app-site-association")
async def apple_app_site_association(settings: Settings = Depends(get_settings_dep)) -> JSONResponse:
    if not (settings.apple_team_id and settings.ios_bundle_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "iOS universal links are not configured")
    return JSONResponse(
        {
            "applinks": {
                "details": [
                    {
                        "appIDs": [f"{settings.apple_team_id.upper()}.{settings.ios_bundle_id}"],
                        "components": [{"/": f"{path}*"} for path in APP_LINK_PATHS],
                    }
                ]
            }
        },
        headers=_WELL_KNOWN_CACHE,
    )


def _html(page: str, nonce: str) -> HTMLResponse:
    return HTMLResponse(
        page,
        headers={
            "Content-Security-Policy": (
                f"default-src 'none'; script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'; "
                "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
            ),
            "Cache-Control": "no-store",
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
            "X-Robots-Tag": "noindex",
        },
    )


@router.get("/verify-email", response_class=HTMLResponse)
async def verify_email_web(settings: Settings = Depends(get_settings_dep)) -> HTMLResponse:
    nonce = new_nonce()
    return _html(verify_email_page(nonce, settings.app_scheme), nonce)


@router.get("/reset-password", response_class=HTMLResponse)
async def reset_password_web(settings: Settings = Depends(get_settings_dep)) -> HTMLResponse:
    nonce = new_nonce()
    return _html(reset_password_page(nonce, settings.app_scheme), nonce)
