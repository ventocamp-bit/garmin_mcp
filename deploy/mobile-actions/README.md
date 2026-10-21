# Private Garmin GPT for ChatGPT mobile

## Optional full Garmin access

The default deployment below remains read-only. After the owner explicitly
authorizes Garmin write access, add `compose.full-access.yaml` to the Compose
command. Set `GARMIN_FULL_TOOLS` to the complete inspected tool-name list from
that release and `GARMIN_FULL_READ_TOOLS` to its reviewed read-only subset.
Both values are comma-separated. The API still verifies the exact catalog.

The full-access schema keeps the existing 19 direct read operations and adds
`list_garmin_tools`, `read_garmin_data`, and `change_garmin_data`. Discovery can
search descriptions, paginate results, or return one tool's exact input schema.
The two dispatch actions accept a tool name and a JSON-encoded argument object.
The server rejects write operations sent through the read action. Writes and
deletes are exported with `x-openai-isConsequential: true`.

Reimport `/openapi.json` in the private GPT after changing access mode. Updating
the server alone does not update a saved GPT Action schema or its API key.

Initialize the `garmin-tokens` and `garmin-files` volumes with owner UID/GID
65532 and directory mode 700. Token files use mode 600. The API-key secret must
also be readable by UID 65532 without being world-readable. Initialize
`/var/lib/garmin/files/.fit-config.json` with
`{"download_dir":"/var/lib/garmin/files"}`. The full-access backend runs with a
read-only root filesystem. File-based course uploads and activity downloads use
the shared Garmin files directory; API calls cannot read arbitrary server
files. GPX uploads require a GPX file already present in that directory.

Validate full access without mutating an account: run the unit tests, verify
the catalog and consequential flags, then call `get_activities` through the
public API with the bearer key. Write-routing tests use a fake backend.

This deployment keeps the raw Garmin MCP on a private container network and
publishes only an API-key-protected, read-only REST/OpenAPI adapter. A private
Custom GPT can import that schema as a GPT Action and can then be opened from
the native ChatGPT mobile app.

## Security boundary

- The MCP service has no host port and no HTTP authentication of its own.
- Both services receive one exact 19-tool allowlist. The Action service refuses
  to start if the MCP exposes a missing, extra, unannotated, or destructive tool.
- The Action endpoint requires a bearer API key of at least 32 characters.
- Requests are limited to 64 KiB, four concurrent calls, and a 60-second call
  timeout. Uvicorn access logging is disabled by default.
- `/openapi.json`, `/healthz`, and `/privacy` are public but contain no Garmin
  records or secrets. Every tool operation is authenticated.
- The loopback port still needs a trusted HTTPS reverse proxy or a managed
  platform HTTPS edge. Do not expose port 8000 or the `/mcp` endpoint.

If the Action API key leaks, an attacker can read every Garmin field exposed by
the 19 tools until the key is rotated. If the Garmin token volume leaks, the
impact can be broader because those bearer tokens authorize the Garmin account.
Keep both secrets outside the repository and restrict host/provider access.

## Values required at deployment time

Create a random API key in a file **outside this repository**. For example, in
PowerShell (choose your own absolute path):

```powershell
$garminSecretPath = 'C:\secure\garmin-action-api-key.txt'
$garminSecretBytes = [byte[]]::new(32)
[Security.Cryptography.RandomNumberGenerator]::Fill($garminSecretBytes)
$garminSecret = [Convert]::ToHexString($garminSecretBytes).ToLowerInvariant()
[IO.File]::WriteAllText($garminSecretPath, $garminSecret)
$env:GARMIN_ACTION_API_KEY_FILE_PATH = $garminSecretPath
$env:GARMIN_ACTION_PUBLIC_BASE_URL = 'https://garmin.example.com'
```

Do not paste the key into chat or commit it. The same key is entered once in the
private GPT Action authentication settings.

The named `garmin-tokens` volume must contain the token files from the existing
Garmin token directory before startup. Copying them to a cloud host is a
security-sensitive external action and is intentionally not automated here.
Do not configure `GARMIN_EMAIL` or `GARMIN_PASSWORD` in the cloud composition.

## Preflight and start

From the repository root:

```powershell
docker compose -f deploy/mobile-actions/compose.yaml config
docker compose -f deploy/mobile-actions/compose.yaml up --build -d
```

When Caddy already runs as a container on the external `n8n_default` network,
include the provided override so only the authenticated Action service joins
that proxy network:

```powershell
docker compose `
  -f deploy/mobile-actions/compose.yaml `
  -f deploy/mobile-actions/compose.caddy.yaml `
  config
```

Then add a Caddy site block pointing at the Docker network alias:

```caddyfile
garmin.example.com {
    reverse_proxy garmin-actions:8080
}
```

Validate the merged Compose model and Caddy configuration before applying
either. Back up and compare the existing Caddyfile hash immediately before an
edit; do not replace existing site blocks.

After the HTTPS edge is configured, these probes should behave as follows:

```text
GET  https://garmin.example.com/healthz                 -> 200, no Garmin data
GET  https://garmin.example.com/openapi.json            -> 200, 19 operations
GET  https://garmin.example.com/privacy                  -> 200, privacy text only
POST https://garmin.example.com/v1/tools/get_full_name  -> 401 without the key
POST https://garmin.example.com/v1/tools/get_full_name  -> 200 with Bearer key
```

## Add it to a private GPT

1. On the ChatGPT web app, create or edit a GPT and keep sharing set to private.
2. Add an Action and import `https://garmin.example.com/openapi.json`.
3. Set Action authentication to API key, authentication type **Bearer**, and
   paste the value from the external key file.
4. Set the privacy-policy URL to `https://garmin.example.com/privacy`.
5. Test only a harmless read such as `get_full_name` first.
6. Open that same private GPT in the native ChatGPT mobile app and repeat the
   harmless read while the PC is off.

Creating/editing GPTs is a web capability; using an existing GPT is supported
on mobile. Custom MCP apps themselves are not currently the native-mobile path,
which is why this adapter uses GPT Actions instead:

- <https://help.openai.com/en/articles/9442513>
- <https://help.openai.com/de-de/articles/8554397-gpts-erstellen-und-bearbeiten>
- <https://help.openai.com/en/articles/12584461-developer-mode-apps-and-full-mcp-connectors-in-chatgpt-beta>

## Rollback

Stop the public service, revoke/remove the Action API key from the GPT, then
remove the HTTPS route. Preserve the Garmin token volume unless you intentionally
want to reauthenticate. No Garmin write operation is included in this deployment.
