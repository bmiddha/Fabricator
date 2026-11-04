# Optional direct DNS management

This is an **opt-in deployment**, separate from Fabricator's default Compose
stack. It implements the approved direct-DNS plan: Fabricator or an unmodified
ExternalDNS client talks to a thin Go webhook; that service alone holds provider
credentials. There is **no Kubernetes DNSEndpoint mode** and no Kubernetes
cluster is required.

Choose **one** provider profile. Nothing starts until explicitly requested.
The webhook defaults to loopback when run directly. In this recipe it listens
inside its private Compose network, published only on host loopback. The
PowerDNS management API has **no published port**. Only authoritative DNS is
published publicly, on **both TCP and UDP port 53**.

## Configuration and trust boundary

| Webhook environment | Meaning |
| --- | --- |
| `DNS_PROVIDER` | Required: `cloudflare` or `powerdns` |
| `DNS_ZONES` | Required comma-separated actual authoritative zone names; never inferred from caller data |
| `DNS_DOMAIN_FILTER` | Optional comma-separated allowed zone/subtree roots; defaults to `DNS_ZONES`. Each must be inside an allowed zone |
| `DNS_LISTEN` | Default `127.0.0.1:8888`; recipe sets `:8888` for container-to-container calls |
| `CLOUDFLARE_API_TOKEN` | Cloudflare only: scoped API token with Zone:Read and DNS:Edit for exactly the permitted zones |
| `POWERDNS_API_URL` | PowerDNS only: origin URL, e.g. `http://powerdns:8081` (no `/api/v1` suffix) |
| `POWERDNS_API_KEY` | PowerDNS only: private API key |
| `POWERDNS_SERVER_ID` | PowerDNS server identifier; default `localhost` |

Flask needs **only the webhook URL**, enabled state, allowed domains, and
ordinary reconciliation/ownership configuration. Never put Cloudflare or
PowerDNS credentials into Flask settings or DNS request payloads.

The service deliberately requires no custom caller authentication headers:
unmodified ExternalDNS supplies none. **Access to the webhook is write access
to its configured DNS scope.** Never expose port 8888 or PowerDNS port 8081
publicly. Limit container network membership to trusted services. Local users
can also reach the host-loopback publication; remove the `ports` stanza if that
is inappropriate. For remote callers use a private VPN or authenticated
reverse proxy supporting the client's standard TLS/auth configuration.
No wildcard CORS access is enabled. Provider API redirects are not followed.
Use HTTPS for non-private PowerDNS endpoints.

The recipe uses `DNS_ZONE` (singular) for a single-zone bootstrap and maps that
to webhook `DNS_ZONES`. Run a separate, independently scoped instance for each
credential boundary. A standalone webhook also supports multiple zones.

## Cloudflare

Export credentials from your secret manager or shell; don't commit them:

```sh
export DNS_ZONE=example.org
export DNS_DOMAIN_FILTER=games.example.org
export CLOUDFLARE_API_TOKEN='replace-with-scoped-token'
docker compose -f deploy/dns/compose.yaml --profile cloudflare up -d --build
```

Use `http://127.0.0.1:8888` from the host or
`http://dns-cloudflare:8888` from a trusted container on
`fabricator-dns-control`. All records created by the service are **DNS-only**
(`proxied:false`), including the special Cloudflare Tunnel CNAME case that
upstream libdns otherwise proxies automatically. Existing, unchanged proxied
records are not silently adopted or rewritten. Do not mix manually proxied
records into RRsets owned by this manager.

Cloudflare uses pinned `github.com/libdns/cloudflare v0.2.2` and
`libdns v1.1.1`. Its `SetRecords` loose matching is deliberately not used.
The service filters upstream SRV and MX deletion lookups by full RDATA to avoid
deleting sibling values with different ports or MX preferences. Provider
account restrictions still apply, including TTL limits and apex/delegation NS
restrictions; rejected changes remain visible as failures.

## Self-hosted PowerDNS Authoritative

Prerequisites:

* A static, publicly routable address; inbound **TCP and UDP 53** allowed.
* No conflicting host DNS listener on the selected bind address.
* Registrar/parent delegation to your real authoritative nameservers, with
  glue A/AAAA records when the nameservers are inside the delegated zone.
* Prefer two authoritative servers on independent networks for production.
  This recipe provides **one primary authoritative instance**, not automatic
  secondary replication, recursion, registrar configuration, DNSSEC key
  management, or high availability.

```sh
export DNS_ZONE=example.org
export DNS_DOMAIN_FILTER=games.example.org
export DNS_NAMESERVERS=ns1.example.org,ns2.example.org
export POWERDNS_API_KEY='replace-with-long-random-secret'
docker compose -f deploy/dns/compose.yaml --profile powerdns up -d --build
```

Use a long randomly generated key containing letters, digits, or `_+=/.-`.
The startup wrapper refuses a missing or config-injection-capable key before
PowerDNS starts; no unauthenticated API fallback is enabled.

`DNS_NAMESERVERS` must name servers that really serve your zone; listing an
unconfigured secondary does not configure it. Set `DNS_SOA_MAILBOX` optionally
to the DNS-name form of the responsible mailbox (`hostmaster.example.org`,
not `hostmaster@example.org`).

The pinned official PowerDNS **5.1.5** image initializes its SQLite schema.
The `powerdns-data` named volume preserves the authoritative database across
container replacements/restarts. A one-shot bootstrap waits for PowerDNS health,
creates a **Native** zone with explicit SOA and NS RRsets, then allows the
webhook to start. Existing zones are left completely unchanged; bootstrap
does not repair or overwrite existing records. API changes update SOA serials
through PowerDNS's `soa_edit_api=DEFAULT`. Zone transfers are disabled by
default; configure them deliberately when adding real secondary servers.

Use `http://dns-powerdns:8888` on the private network, or loopback from the host.
PowerDNS itself listens on unprivileged container port 5353, forwarded to host
port 53. `DNS_BIND_ADDRESS` defaults to `0.0.0.0`; set a specific public IPv4
address if needed. Explicit IPv6 publications/firewall rules must be configured
for IPv6 service. `DNS_PUBLIC_PORT` defaults to 53; use a high port only for
testing. `DNS_WEBHOOK_PORT` defaults to 8888.

To attach an existing trusted Fabricator container, add the external network
to that service in a **local override** (this recipe does not edit the default
stack):

```yaml
services:
  backend: # replace with your actual Fabricator service name
    networks: [default, dns-control]
networks:
  dns-control:
    external: true
    name: fabricator-dns-control
```

`DNS_NETWORK_NAME` overrides the network name when running independent stacks.
Keep this network private; the PowerDNS API key is a provider credential, not
an authorization check on the webhook.

Inspect and back up the PowerDNS database using the image's `pdnsutil zone
export` command or a consistent SQLite backup while coordinating writers.
Preserve the named volume. `docker compose down` keeps it;
**`down -v` permanently deletes authoritative data**. Do not run that in normal
shutdown or upgrades. The webhook needs no persistent storage: reconciliation
and ownership are the caller's responsibility.

## Wire contract

Pinned compatibility target: **ExternalDNS v0.24.0**, Go **1.27.3**.
The actual upstream webhook client is exercised in the Go test suite.

Request/response media type (including punctuation, with no charset suffix):

```text
application/external.dns.webhook+json;version=1
```

| Request | Successful response |
| --- | --- |
| `GET /`, `Accept: <media type>` | 200, upstream DomainFilter JSON: `{"include":["games.example.org"]}` |
| `GET /records` | 200, array of upstream ExternalDNS Endpoints, including `[]` for an empty scope |
| `POST /adjustendpoints`, Endpoint array | 200, validated/canonicalized Endpoint array |
| `POST /records`, upstream plan.Changes | **204**, empty body |

POST requests accept the negotiated media type or `application/json`.
Negotiation happens independently of whether records exist. `GET /records`
fails closed if a provider read fails; it never reports provider failure as an
empty zone. POST bodies are limited to 1 MiB, must contain one JSON document,
and reject unknown fields. Unsupported routes/methods are not aliases.

Endpoints use `dnsName`, `recordType`, `recordTTL`, and `targets`:

* A/AAAA: canonical address strings.
* CNAME/NS: lowercase absolute target names with trailing dot.
* SRV: `priority weight port target.`; MX: `preference target.`.
* TXT: **raw text**, not surrounding DNS quotes. A literal `"` in text remains
  part of the text; the PowerDNS adapter handles quoting/escaping internally.
  Multi-string provider TXT RDATA is concatenated canonically; writes are
  segmented into 255-byte strings as needed for PowerDNS.
* CAA: `flags tag "value"` using DNS presentation syntax.

Names are lowercase without a trailing dot. TTL `0`/omitted defaults to 300;
explicit TTL must fit uint32 and satisfy the provider's limits. Supported
types are **A, AAAA, CNAME, SRV, TXT, MX, CAA, NS**, where permitted by the
provider. SOA and unsupported record types are never managed. Routing/set
identifiers and provider-specific options are rejected rather than ignored.
Control-character injection, empty target lists, duplicate values, CNAME
multi-target/apex/coexistence conflicts, and out-of-scope writes are rejected.

Changes example:

```json
{
  "create": [
    {"dnsName":"mc.games.example.org","recordType":"A","recordTTL":300,"targets":["192.0.2.10"]},
    {"dnsName":"_fabricator-a.mc.games.example.org","recordType":"TXT","recordTTL":300,"targets":["heritage=external-dns,external-dns/owner=fabricator"]}
  ]
}
```

Updates use paired `updateOld`/`updateNew` arrays in matching order; deletion
uses `delete`. Updates cannot rename or change type (use delete/create).
Each RRset can appear only once per request. A create requires absence;
an update/delete requires the complete current RRset **including TTL and all
values** to equal `updateOld`/`delete`. This intentionally refuses to overwrite
or remove unmentioned sibling values. The client must refresh before retrying
a stale operation; ownership TXT records are managed by the caller, not
implicitly injected by this service.

Validation and previous-value checks run before any writes. Changes are
serialized within one service process, re-read immediately before each write,
and read back after writing. **Neither provider offers a transactional,
cross-record compare-and-swap through this adapter.** An external writer can
still race the final check. Run one service/reconciler per owned scope and
do not have independent actors editing the same RRsets.

PowerDNS atomically replaces/deletes one RRset with a REST PATCH and preserves
other names/types. Disabled records cannot be represented losslessly and
cause listing to fail closed, rather than being silently re-enabled/deleted.
The adapter compensates for PowerDNS 5.1's two-pass RFC1035 escape
normalization so literal TXT quotes, backslashes, and non-ASCII bytes survive.
Cloudflare changes individual records, adding new values before removing old
ones except a CNAME target replacement (Cloudflare permits only one CNAME).
A CNAME target change or TTL change for an existing value requires
delete/recreate through libdns, so can briefly remove that value. Cross-RRset operations and
Cloudflare multi-value operations are **not atomic** and are not rolled back.

Error responses:

* 400 invalid change; 406 incompatible negotiation; 413 excessive body;
  415 unsupported POST media type.
* 409 stale previous RRset/existing create: no writes attempted.
* 502 provider failure: includes `applied` RRset keys and `failed` key after
  mutation begins. The failed RRset may itself have partially changed.
  Provider failure before writes is explicitly identified.

ExternalDNS sees a non-success status (never a false 204); detailed partial
results are in the response JSON and service logs. **Refresh records and
reconcile the observed state after any failure. Do not blindly replay a
partially applied batch.**

## Verification

```sh
cd dns-provider
go test -race ./...
go vet ./...
cd ..
DNS_ZONE=example.org docker compose -f deploy/dns/compose.yaml --profile powerdns config --quiet
curl -H 'Accept: application/external.dns.webhook+json;version=1' http://127.0.0.1:8888/
curl http://127.0.0.1:8888/records
dig @127.0.0.1 example.org SOA
dig +tcp @127.0.0.1 example.org NS
```

Example unmodified ExternalDNS flags: `--provider=webhook`,
`--webhook-provider-url=http://dns-powerdns:8888`,
`--domain-filter=games.example.org`, and a stable TXT registry owner ID.
Configure its source separately if using ExternalDNS itself. Fabricator uses
the same HTTP wire protocol directly; it does not need ExternalDNS installed.
