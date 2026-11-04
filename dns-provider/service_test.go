package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"sigs.k8s.io/external-dns/endpoint"
	"sigs.k8s.io/external-dns/pkg/apis/externaldns"
	"sigs.k8s.io/external-dns/plan"
	"sigs.k8s.io/external-dns/provider/webhook"
)

type memoryBackend struct {
	rows   map[string]RRSet
	writes int
	failAt int
}

func (b *memoryBackend) List(_ context.Context, zone string) ([]RRSet, error) {
	result := []RRSet{}
	for _, rr := range b.rows {
		if within(canonicalName(rr.Name), zone) {
			copyRR := rr
			copyRR.Values = append([]string{}, rr.Values...)
			if rr.Type == "TXT" {
				for i, value := range copyRR.Values {
					copyRR.Values[i] = txtRDATA(value)
				}
			}
			result = append(result, copyRR)
		}
	}
	return result, nil
}
func (b *memoryBackend) Replace(_ context.Context, _ string, old, next RRSet) error {
	b.writes++
	if b.writes == b.failAt {
		return errors.New("injected provider failure")
	}
	delete(b.rows, key(old))
	if len(next.Values) > 0 {
		b.rows[key(next)] = next
	}
	return nil
}
func testService() (*Service, *memoryBackend) {
	b := &memoryBackend{rows: map[string]RRSet{}}
	return &Service{backend: b, zones: []string{"example.org"}, domains: []string{"games.example.org"}}, b
}
func post(t *testing.T, s *Service, path string, body any) *httptest.ResponseRecorder {
	t.Helper()
	data, err := json.Marshal(body)
	if err != nil {
		t.Fatal(err)
	}
	req := httptest.NewRequest("POST", path, bytes.NewReader(data))
	req.Header.Set("Content-Type", mediaType)
	w := httptest.NewRecorder()
	s.Handler().ServeHTTP(w, req)
	return w
}
func ep(name, typ string, values ...string) *endpoint.Endpoint {
	return endpoint.NewEndpointWithTTL(name, typ, 300, values...)
}

func TestPinnedExternalDNSClient(t *testing.T) {
	s, b := testService()
	server := httptest.NewServer(s.Handler())
	defer server.Close()
	p, err := webhook.New(context.Background(), &externaldns.Config{WebhookProviderURL: server.URL,
		WebhookProviderReadTimeout: time.Second, WebhookProviderWriteTimeout: time.Second, WebhookProviderMaxBodySize: 1 << 20}, nil)
	if err != nil {
		t.Fatal(err)
	}
	if !p.GetDomainFilter().Match("mc.games.example.org") || p.GetDomainFilter().Match("other.example.org") {
		t.Fatal("wrong negotiated domain filter")
	}
	endpoints, err := p.AdjustEndpoints([]*endpoint.Endpoint{ep("MC.GAMES.EXAMPLE.ORG.", "A", "192.0.2.1")})
	if err != nil || len(endpoints) != 1 || endpoints[0].DNSName != "mc.games.example.org" {
		t.Fatalf("adjust: %v %+v", err, endpoints)
	}
	if err := p.ApplyChanges(context.Background(), &plan.Changes{Create: endpoints}); err != nil {
		t.Fatal(err)
	}
	listed, err := p.Records(context.Background())
	if err != nil || len(listed) != 1 {
		t.Fatalf("list: %v %+v", err, listed)
	}
	next := ep("mc.games.example.org", "A", "192.0.2.2", "192.0.2.3")
	if err := p.ApplyChanges(context.Background(), &plan.Changes{UpdateOld: listed, UpdateNew: []*endpoint.Endpoint{next}}); err != nil {
		t.Fatal(err)
	}
	if err := p.ApplyChanges(context.Background(), &plan.Changes{Delete: []*endpoint.Endpoint{next}}); err != nil {
		t.Fatal(err)
	}
	if len(b.rows) != 0 {
		t.Fatal(b.rows)
	}
}

func TestRecordTypesAndConversions(t *testing.T) {
	cases := []struct{ typ, name, value string }{
		{"A", "mc", "192.0.2.4"}, {"AAAA", "mc", "2001:db8::4"},
		{"CNAME", "alias", "mc.games.example.org."},
		{"SRV", "_minecraft._tcp", "0 5 25565 mc.games.example.org."},
		{"TXT", "_owner", `owner with "quotes" and \slash`},
		{"MX", "mail", "10 mail.example.org."}, {"CAA", "ca", `0 issue "letsencrypt.org"`},
		{"NS", "delegated", "ns.example.org."},
	}
	for _, c := range cases {
		t.Run(c.typ, func(t *testing.T) {
			s, _ := testService()
			r, err := s.normalize(ep(c.name+".games.example.org", c.typ, c.value))
			if err != nil {
				t.Fatal(err)
			}
			if r.Values[0] != c.value {
				t.Fatalf("canonical RDATA %q != %q", r.Values[0], c.value)
			}
			record, err := toLibDNS("example.org", r, r.Values[0])
			if err != nil {
				t.Fatal(err)
			}
			roundTrip, err := fromLibDNS("example.org", record)
			if err != nil {
				t.Fatal(err)
			}
			normalized, err := normalizeValue(r.Name, r.Type, roundTrip.Values[0], r.TTL)
			if err != nil || normalized != r.Values[0] || roundTrip.Name != r.Name {
				t.Fatalf("roundtrip: %+v %q %v", roundTrip, normalized, err)
			}
			if w := post(t, s, "/records", plan.Changes{Create: []*endpoint.Endpoint{ep(r.Name, r.Type, r.Values...)}}); w.Code != 204 {
				t.Fatalf("%d: %s", w.Code, w.Body)
			}
			listed := httptest.NewRecorder()
			s.Handler().ServeHTTP(listed, httptest.NewRequest("GET", "/records", nil))
			var values []*endpoint.Endpoint
			if err := json.Unmarshal(listed.Body.Bytes(), &values); err != nil {
				t.Fatal(err)
			}
			if len(values) != 1 || len(values[0].Targets) != 1 || values[0].Targets[0] != r.Values[0] {
				t.Fatalf("GET canonicalization differs: %s", listed.Body)
			}
		})
	}
}

func TestScopeAndValidation(t *testing.T) {
	cases := []*endpoint.Endpoint{
		nil, ep("evilgames.example.org", "A", "192.0.2.1"), ep("games.example.org.evil", "A", "192.0.2.1"),
		ep("other.example.org", "A", "192.0.2.1"), ep("x.games.example.org", "A", "not-an-IP"),
		ep("x.games.example.org", "AAAA", "192.0.2.1"), ep("x.games.example.org", "SOA", "bad"),
		ep("x.games.example.org", "MX", "65536 target."), ep("x.games.example.org", "SRV", "0 0 65536 target."),
		ep("x.games.example.org", "CAA", `999 issue "ca"`), ep("x.games.example.org", "CNAME", "a.", "b."),
		ep("x.games.example.org", "A", "192.0.2.1", "192.0.2.1"),
		ep("a..games.example.org", "A", "192.0.2.1"),
		ep("x.games.example.org", "TXT", "\"injected\"\nother. IN A 192.0.2.5"),
	}
	for i, record := range cases {
		s, b := testService()
		w := post(t, s, "/records", plan.Changes{Create: []*endpoint.Endpoint{record}})
		if w.Code != 400 || b.writes != 0 {
			t.Fatalf("case %d: %d %s writes=%d", i, w.Code, w.Body, b.writes)
		}
	}
}

func TestPreviousValuesPreventDestructiveReplacement(t *testing.T) {
	s, b := testService()
	b.rows["mc.games.example.org/A"] = RRSet{Name: "mc.games.example.org", Type: "A", TTL: 300, Values: []string{"192.0.2.1", "192.0.2.2"}}
	old := ep("mc.games.example.org", "A", "192.0.2.1")
	next := ep("mc.games.example.org", "A", "192.0.2.3")
	for _, c := range []plan.Changes{
		{Create: []*endpoint.Endpoint{next}}, {Delete: []*endpoint.Endpoint{old}},
		{UpdateOld: []*endpoint.Endpoint{old}, UpdateNew: []*endpoint.Endpoint{next}},
	} {
		w := post(t, s, "/records", c)
		if w.Code != 409 || b.writes != 0 {
			t.Fatalf("%d %s", w.Code, w.Body)
		}
	}
}
func TestPreflightAndPartialFailures(t *testing.T) {
	s, b := testService()
	c := plan.Changes{Create: []*endpoint.Endpoint{ep("a.games.example.org", "A", "192.0.2.1"), ep("b.games.example.org", "A", "192.0.2.2")}}
	b.failAt = 2
	w := post(t, s, "/records", c)
	if w.Code != 502 || !strings.Contains(w.Body.String(), `"applied":["a.games.example.org/A"]`) || !strings.Contains(w.Body.String(), `"failed":"b.games.example.org/A"`) {
		t.Fatalf("%d %s", w.Code, w.Body)
	}
	if len(b.rows) != 1 {
		t.Fatal(b.rows)
	}
	s, b = testService()
	c.Create = append(c.Create, ep("c.outside.org", "A", "192.0.2.3"))
	if w = post(t, s, "/records", c); w.Code != 400 || b.writes != 0 {
		t.Fatalf("%d %s", w.Code, w.Body)
	}
}
func TestCNAMEConflictAndTransition(t *testing.T) {
	s, b := testService()
	old := ep("mc.games.example.org", "A", "192.0.2.1")
	next := ep("mc.games.example.org", "CNAME", "target.example.org.")
	b.rows["mc.games.example.org/A"] = RRSet{Name: old.DNSName, Type: "A", TTL: 300, Values: []string{"192.0.2.1"}}
	if w := post(t, s, "/records", plan.Changes{Create: []*endpoint.Endpoint{next}}); w.Code != 400 || b.writes != 0 {
		t.Fatalf("%d %s", w.Code, w.Body)
	}
	if w := post(t, s, "/records", plan.Changes{Delete: []*endpoint.Endpoint{old}, Create: []*endpoint.Endpoint{next}}); w.Code != 204 {
		t.Fatalf("%d %s", w.Code, w.Body)
	}
}
func TestProtocolErrors(t *testing.T) {
	s, _ := testService()
	for _, tc := range []struct {
		method, path, content, accept, body string
		code                                int
	}{
		{"GET", "/", "", "application/json", "", 406},
		{"POST", "/records", "", "", "{}", 415},
		{"POST", "/records", mediaType, "", "{} {}", 400},
		{"POST", "/records", mediaType, "", `{"typo":[]}`, 400},
		{"POST", "/records", mediaType, "", `{"create":[null]}`, 400},
		{"POST", "/records", mediaType, "", `{"create":[` + strings.Repeat(" ", 1<<20) + `]}`, 413},
		{"POST", "/adjustendpoints", mediaType, "", "null", 200},
		{"POST", "/", mediaType, "", "{}", 405},
		{"GET", "/unknown", "", "", "", 404},
	} {
		req := httptest.NewRequest(tc.method, tc.path, strings.NewReader(tc.body))
		req.Header.Set("Content-Type", tc.content)
		req.Header.Set("Accept", tc.accept)
		w := httptest.NewRecorder()
		s.Handler().ServeHTTP(w, req)
		if w.Code != tc.code {
			t.Fatalf("%s %s: %d != %d %s", tc.method, tc.path, w.Code, tc.code, w.Body)
		}
	}
}

func TestConfig(t *testing.T) {
	t.Setenv("DNS_PROVIDER", "cloudflare")
	t.Setenv("CLOUDFLARE_API_TOKEN", "test-token")
	t.Setenv("DNS_ZONES", "EXAMPLE.ORG.")
	t.Setenv("DNS_DOMAIN_FILTER", "games.example.org")
	c, err := loadConfig()
	if err != nil || c.Listen != "127.0.0.1:8888" || c.Zones[0] != "example.org" {
		t.Fatalf("%+v %v", c, err)
	}
	t.Setenv("DNS_DOMAIN_FILTER", "outside.org")
	if _, err := loadConfig(); err == nil {
		t.Fatal("accepted out-of-zone scope")
	}
	t.Setenv("DNS_ZONES", "")
	if _, err := loadConfig(); err == nil {
		t.Fatal("accepted empty scope")
	}
}
