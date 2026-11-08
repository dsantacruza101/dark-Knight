"""Julia Pennyworth: parsers del inventario, correlacion KEV/USN/Node,
deduplicacion y salida, con fixtures JSON pequenos (sin red).

Correr: venv/bin/python -m pytest tests/ -q  (o venv/bin/python -m unittest discover tests)
"""

import json
import os
import sys
import tempfile
import time
import unittest
import urllib.error
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import julia  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "julia"
TODAY = date(2026, 11, 8)
CONFIG = {**julia.DEFAULTS}


def fixture(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def pkg(version: str, source: str, source_version: str | None = None) -> dict:
    return {"version": version, "source": source, "source_version": source_version or version}


PACKAGES = {
    "linux-image-7.0.0-43-generic": pkg("7.0.0-43.43", "linux-signed"),
    "linux-image-7.0.0-44-generic": pkg("7.0.0-44.44", "linux-signed"),
    "linux-image-generic": pkg("7.0.0.43.43", "linux-meta"),
    "openssh-server": pkg("1:10.0p1-5ubuntu2", "openssh"),
    "openssh-client": pkg("1:10.0p1-5ubuntu2", "openssh"),
    "openssl": pkg("3.5.3-1ubuntu2", "openssl"),
    "libssl3t64": pkg("3.5.3-1ubuntu2", "openssl"),
    "sudo": pkg("1.9.17p2-1ubuntu1", "sudo"),
    "curl": pkg("8.14.1-2ubuntu1", "curl"),
    "docker-ce": pkg("5:28.3.0-1~ubuntu.26.04~resolute", "docker-ce"),
    "containerd.io": pkg("2.1.4-1~ubuntu.26.04~resolute", "containerd.io"),
}


def container(name: str, age_days: float, image: str | None = None, has_build: bool | None = True,
              project: str | None = None, files: str = "docker-compose.server.yml", service: str = "app") -> dict:
    root = f"/home/dsantacruz/projects/{project or name}"
    return {
        "name": name, "image": image or f"{name}:latest", "image_id": "sha256:abc", "created": "2026-10-01T00:00+00:00",
        "age_days": age_days, "compose_dir": root, "compose_service": service,
        "compose_files": ",".join(f"{root}/{f}" for f in files.split(",")), "has_build": has_build,
    }


def inventory(**overrides) -> dict:
    app, legacy = container("portfolio", 30.0), container("legacy", 3.0)
    inv = {
        "collected_at": "2026-11-08T13:50:00+00:00",
        "kernel": julia.kernel_state("7.0.0-43-generic", PACKAGES),
        "reboot": {"required": False, "days": None, "since": None, "packages": []},
        "packages": PACKAGES,
        "upgradable": [],
        "node": [
            {"where": "host", "version": "v22.23.4"},
            {"where": "docker:portfolio", "version": "v22.23.1", "container": app},
            {"where": "docker:legacy", "version": "v20.19.5", "container": legacy},
        ],
        "containers": [app, legacy],
        "versions": {"cloudflared": "2026.10.0", "nginx": "1.28.0", "openssh-server": "1:10.0p1-5ubuntu2", "docker": "28.3.0"},
        "mptcp": "1",
        "errors": [],
    }
    inv.update(overrides)
    return inv


class VersionTests(unittest.TestCase):
    def test_dpkg_compare(self):
        cases = [
            ("1.0~rc1", "1.0", -1), ("1:1.0", "2.0", 1), ("7.0.0-43.43", "7.0.0-44.44", -1),
            ("2.0-1ubuntu1", "2.0-1", 1), ("1.0a", "1.0", 1), ("3.5.3-1ubuntu3", "3.5.3-1ubuntu3", 0),
            ("1:10.0p1-5ubuntu2", "1:10.0p1-5ubuntu1", 1), ("1.10", "1.9", 1),
        ]
        for a, b, expected in cases:
            with self.subTest(a=a, b=b):
                self.assertEqual(julia.dpkg_compare(a, b), expected)
                self.assertEqual(julia.dpkg_compare(b, a), -expected)

    def test_upstream_core(self):
        self.assertEqual(julia.upstream_core("5:28.0.1-1~ubuntu.26.04~resolute"), "28.0.1")
        self.assertEqual(julia.upstream_core("26.1.3+dfsg1-0ubuntu1"), "26.1.3")

    def test_node_version(self):
        self.assertEqual(julia.parse_node_version("v22.23.4\n"), (22, 23, 4))
        self.assertIsNone(julia.parse_node_version("node: not found"))


class InventoryParserTests(unittest.TestCase):
    def test_dpkg_query_only_installed(self):
        out = (
            "ii \topenssl\t3.5.3-1ubuntu2\topenssl\t3.5.3-1ubuntu2\n"
            "rc \told-pkg\t1.0\told-pkg\t1.0\n"
            "ii \tlibssl3t64\t3.5.3-1ubuntu2\topenssl\t3.5.3-1ubuntu2\n"
            "ii \tcurl\t8.14.1-2ubuntu1\t\t\n"
        )
        packages = julia.parse_dpkg_query(out)
        self.assertEqual(sorted(packages), ["curl", "libssl3t64", "openssl"])
        self.assertEqual(packages["libssl3t64"]["source"], "openssl")
        self.assertEqual(packages["curl"]["source"], "curl")
        self.assertEqual(packages["curl"]["source_version"], "8.14.1-2ubuntu1")

    def test_kernel_state(self):
        k = julia.kernel_state("7.0.0-43-generic", PACKAGES)
        self.assertEqual(k["running_version"], "7.0.0-43.43")
        self.assertEqual(k["newest_package"], "linux-image-7.0.0-44-generic")
        self.assertEqual(k["flavor"], "generic")

    def test_reboot_state_days_from_mtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "reboot-required"
            path.write_text("*** System restart required ***\n")
            (Path(tmp) / "reboot-required.pkgs").write_text("linux-image-7.0.0-44-generic\nlibc6\nlibc6\n")
            mtime = time.time() - 4.5 * 86400
            os.utime(path, (mtime, mtime))
            rb = julia.reboot_state(path)
        self.assertTrue(rb["required"])
        self.assertAlmostEqual(rb["days"], 4.5, delta=0.1)
        self.assertEqual(rb["packages"], ["libc6", "linux-image-7.0.0-44-generic"])
        self.assertFalse(julia.reboot_state(Path(tmp) / "missing")["required"])

    def test_apt_upgradable_and_origin(self):
        out = (
            "Listing...\n"
            "libc6/resolute-updates,resolute-security 2.42-0ubuntu3.1 amd64 [upgradable from: 2.42-0ubuntu3]\n"
            "vim/resolute-updates 2:9.1.0-1ubuntu1.1 amd64 [upgradable from: 2:9.1.0-1ubuntu1]\n"
            "docker-ce/resolute 5:28.5.1-1~ubuntu.26.04~resolute amd64 [upgradable from: 5:28.3.0-1~ubuntu.26.04~resolute]\n"
            "mystery/resolute 1.1 amd64 [upgradable from: 1.0]\n"
            "baseline/resolute 1.1 amd64 [upgradable from: 1.0]\n"
        )
        hosts = {"mystery": "pkg.example.org", "baseline": "archive.ubuntu.com"}
        items = julia.classify_upgradable(julia.parse_apt_upgradable(out), "resolute", ["docker-ce"], hosts.get)
        origins = {i["name"]: i["origin"] for i in items}
        self.assertEqual(origins, {
            "libc6": "ubuntu-security", "vim": "ubuntu-updates", "docker-ce": "third-party",
            "mystery": "third-party", "baseline": "ubuntu-updates",
        })
        self.assertEqual(items[2]["old"], "5:28.3.0-1~ubuntu.26.04~resolute")

    def test_policy_origin(self):
        out = (
            "cloudflared:\n"
            "  Installed: 2026.9.0\n"
            "  Candidate: 2026.10.0\n"
            "  Version table:\n"
            "     2026.10.0 500\n"
            "        500 https://pkg.cloudflare.com/cloudflared any/main amd64 Packages\n"
            " *** 2026.9.0 100\n"
            "        100 /var/lib/dpkg/status\n"
        )
        self.assertEqual(julia.parse_policy_origin(out), "pkg.cloudflare.com")
        self.assertIsNone(julia.parse_policy_origin(""))

    def test_docker_time(self):
        when = julia.parse_docker_time("2026-10-01T12:34:56.123456789Z")
        self.assertEqual(when, datetime(2026, 10, 1, 12, 34, 56, 123456, tzinfo=timezone.utc))
        self.assertIsNone(julia.parse_docker_time("ayer"))


class KevTests(unittest.TestCase):
    def setUp(self):
        self.cves = fixture("ubuntu_cves.json")
        self.lookups = []

        def lookup(cve, old):
            self.lookups.append((cve, old))
            return self.cves.get(cve)

        targets = julia.component_targets(inventory(), CONFIG)
        self.findings, self.stats = julia.kev_findings(fixture("kev.json"), targets, lookup, CONFIG, TODAY)
        self.by_key = {f["key"]: f for f in self.findings}

    def test_product_mapping(self):
        self.assertEqual(julia.kev_component({"vendorProject": "Linux", "product": "Kernel"}), "kernel")
        self.assertIsNone(julia.kev_component({"vendorProject": "Docker", "product": "Desktop"}))
        self.assertIsNone(julia.kev_component({"vendorProject": "Microsoft", "product": "Windows"}))
        self.assertIsNone(julia.kev_component({"vendorProject": "F5", "product": "NGINX Plus"}))
        self.assertEqual(julia.kev_component({"vendorProject": "F5", "product": "NGINX"}), "nginx")

    def test_kernel_fix_installed_but_not_running(self):
        f = self.by_key["kev:CVE-2026-1001:kernel"]
        self.assertEqual(f["severity"], "CRITICAL")
        self.assertTrue(f["immediate"])
        self.assertIn("NO esta corriendo", f["detail"])
        self.assertIn("reboot", f["fix"])

    def test_kernel_fix_not_installed(self):
        f = self.by_key["kev:CVE-2026-1002:kernel"]
        self.assertEqual(f["severity"], "CRITICAL")
        self.assertIn("7.0.0-43.43 < 7.0.0-45.45", f["detail"])
        self.assertIn("full-upgrade", f["fix"])

    def test_third_party_uses_ubuntu_upstream_as_reference(self):
        f = self.by_key["kev:CVE-2026-6001:docker-ce"]
        self.assertEqual(f["severity"], "CRITICAL")
        self.assertIn("docker-ce", f["fix"])

    def test_no_fix_is_high_not_immediate(self):
        f = self.by_key["kev:CVE-2026-5001:sudo"]
        self.assertEqual(f["severity"], "HIGH")
        self.assertFalse(f["immediate"])
        self.assertIn("sudoedit", f["detail"])

    def test_fixed_unmatched_and_old_are_silent(self):
        self.assertNotIn("kev:CVE-2026-2001:openssh", self.by_key)
        self.assertFalse(any("CVE-2026-3001" in k or "CVE-2026-4001" in k or "CVE-2016-5195" in k for k in self.by_key))
        self.assertEqual(self.stats["fixed"], 1)
        self.assertEqual(self.stats["unknown"], 1)
        self.assertEqual(self.stats["matched"], 6)
        self.assertIn(("CVE-2016-5195", True), self.lookups)
        # Mas recientes primero (si se llega al tope de consultas, caen las viejas).
        self.assertEqual(self.lookups[0][0], "CVE-2026-1002")


class NoticeTests(unittest.TestCase):
    def test_notices(self):
        findings = julia.notice_findings(fixture("ubuntu_notices.json"), inventory(), CONFIG)
        by_key = {f["key"]: f for f in findings}
        self.assertEqual(sorted(by_key), ["usn:USN-8001-1", "usn:USN-8002-1"])
        ssl = by_key["usn:USN-8001-1"]
        self.assertEqual(ssl["severity"], "HIGH")
        self.assertEqual(ssl["packages"], ["libssl3t64", "openssl"])
        self.assertIn("--only-upgrade libssl3t64 openssl", ssl["fix"])
        kernel = by_key["usn:USN-8002-1"]
        self.assertIn("falta reiniciar", kernel["detail"])
        self.assertEqual(kernel["fix"], "sudo reboot")
        self.assertNotIn("aws", kernel["detail"])

    def test_notices_pagination(self):
        calls = []

        def fetch(url, timeout):
            calls.append(url)
            offset = int(url.rsplit("offset=", 1)[1])
            size = 20 if offset < 40 else 5
            return {"notices": [{"id": f"USN-{offset + i}"} for i in range(size)], "offset": offset,
                    "limit": 20, "total_results": 45}

        self.assertIn("limit=20", julia.DEFAULTS["feeds"]["notices"])
        with tempfile.TemporaryDirectory() as tmp:
            feeds = julia.FeedCache(Path(tmp), 5, max_cve_lookups=1, fetch=fetch)
            data = julia.fetch_notices(feeds, {**CONFIG, "notices_pages": 5}, timedelta(hours=1))
            self.assertEqual(len(data["notices"]), 45)
            self.assertEqual(len(calls), 3)
            self.assertTrue(calls[0].endswith("release=resolute&limit=20&offset=0"))
            self.assertTrue(calls[2].endswith("offset=40"))

            feeds = julia.FeedCache(Path(tmp) / "b", 5, max_cve_lookups=1, fetch=fetch)
            data = julia.fetch_notices(feeds, {**CONFIG, "notices_pages": 1}, timedelta(hours=1))
            self.assertEqual(len(data["notices"]), 20)

            def down(url, timeout):
                raise urllib.error.URLError("HTTP Error 422: Invalid payload")

            feeds = julia.FeedCache(Path(tmp) / "c", 5, max_cve_lookups=1, fetch=down)
            self.assertIsNone(julia.fetch_notices(feeds, CONFIG, timedelta(hours=1)))

    def test_security_upgradable_not_repeated(self):
        inv = inventory(upgradable=[
            {"name": "openssl", "old": "3.5.3-1ubuntu2", "version": "3.5.3-1ubuntu3", "origin": "ubuntu-security", "origin_host": None},
            {"name": "libxml2", "old": "2.14-1", "version": "2.14-1ubuntu0.1", "origin": "ubuntu-security", "origin_host": None},
            {"name": "cloudflared", "old": "2026.9.0", "version": "2026.10.0", "origin": "third-party", "origin_host": "pkg.cloudflare.com"},
        ])
        findings = {f["key"]: f for f in julia.upgradable_findings(inv, {"openssl", "libssl3t64"})}
        self.assertEqual(findings["apt:third-party"]["severity"], "MEDIUM")
        self.assertIn("cloudflared", findings["apt:third-party"]["fix"])
        self.assertIn("libxml2", findings["apt:ubuntu-security"]["detail"])
        self.assertNotIn("openssl", findings["apt:ubuntu-security"]["detail"])


class NodeAndHostTests(unittest.TestCase):
    def test_node_runtimes(self):
        findings, ok = julia.node_findings(inventory(), fixture("node_index.json"), fixture("node_schedule.json"), CONFIG, TODAY)
        by_key = {f["key"]: f for f in findings}
        self.assertTrue(any(o.startswith("host v22.23.4") for o in ok))
        app = by_key["node:docker:portfolio"]
        self.assertEqual(app["severity"], "HIGH")
        self.assertIn("v22.23.1 < v22.23.4", app["detail"])
        self.assertIn("build --pull app", app["fix"])
        self.assertIn("-f docker-compose.server.yml", app["fix"])
        legacy = by_key["node:docker:legacy"]
        self.assertIn("fin de vida el 2026-04-30", legacy["detail"])
        self.assertIn("node:24", legacy["fix"])
        self.assertNotIn("webhook", app["fix"])

    def test_eol_heuristic_without_schedule(self):
        index = fixture("node_index.json")
        self.assertEqual(julia.node_line_eol(19, None, index), date(2023, 6, 18))
        self.assertEqual(julia.node_line_eol(20, None, index), date(2026, 4, 18))

    def test_reboot(self):
        inv = inventory(reboot={"required": True, "days": 4.2, "since": "2026-11-04T08:00-06:00",
                                "packages": ["linux-image-7.0.0-44-generic", "libc6"]})
        f = julia.reboot_findings(inv, CONFIG)[0]
        self.assertEqual((f["severity"], f["immediate"]), ("HIGH", True))
        inv["reboot"]["days"] = 1.0
        f = julia.reboot_findings(inv, CONFIG)[0]
        self.assertEqual((f["severity"], f["immediate"]), ("MEDIUM", False))
        f = julia.reboot_findings(inventory(), CONFIG)[0]
        self.assertEqual(f["key"], "reboot:kernel")

    def test_mptcp(self):
        self.assertEqual(julia.hardening_findings(inventory())[0]["severity"], "LOW")
        self.assertEqual(julia.hardening_findings(inventory(mptcp="0")), [])
        self.assertEqual(julia.hardening_findings(inventory(mptcp=None)), [])

    def test_read_mptcp(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "enabled"
            path.write_text("1\n")
            self.assertEqual(julia.read_mptcp(path), "1")
            inv = inventory(mptcp=julia.read_mptcp(path))
            self.assertEqual(julia.hardening_findings(inv)[0]["key"], "sysctl:mptcp")
            path.write_text("0\n")
            self.assertEqual(julia.read_mptcp(path), "0")
        self.assertEqual(julia.parse_sysctl_value("net.mptcp.enabled = 1"), "1")
        self.assertIsNone(julia.parse_sysctl_value(""))
        self.assertIsNone(julia.parse_sysctl_value(None))

    def test_container_without_node_logs_debug(self):
        with self.assertLogs("julia", level="DEBUG") as logs:
            self.assertIsNone(julia.run_cmd(["sh", "-c", "exit 127"], expected_codes=(julia.NODE_MISSING,)))
            self.assertIsNone(julia.run_cmd(["sh", "-c", "exit 3"], expected_codes=(julia.NODE_MISSING,)))
        self.assertEqual([r.levelname for r in logs.records], ["DEBUG", "INFO"])


class ImageTests(unittest.TestCase):
    TAGS = {("wazuh", "wazuh-manager", "4.14."): ["4.14.10", "4.14.9", "4.14.8", "4.14.10-rc1"],
            ("wazuh", "wazuh-indexer", "4.14."): ["4.14.8", "4.14.7"]}

    def lookup(self, namespace, repo, prefix):
        self.calls.append((namespace, repo, prefix))
        return self.TAGS.get((namespace, repo, prefix))

    def setUp(self):
        self.calls = []

    def test_compose_update_cmd(self):
        nats = container("nats", 30, image="nats:alpine3.21", has_build=False, project="portfolioServiceLauncher",
                         service="nats-server")
        self.assertEqual(julia.compose_update_cmd(nats, CONFIG),
                         "cd /home/dsantacruz/projects/portfolioServiceLauncher && docker compose -f docker-compose.server.yml "
                         "pull nats-server && docker compose -f docker-compose.server.yml up -d nats-server"
                         "  (en produccion: redeploy via webhook)")
        pg = container("pg", 30, image="pgvector/pgvector:pg16", has_build=False, project="penguin",
                       files="docker-compose.yml", service="postgres")
        cmd = julia.compose_update_cmd(pg, CONFIG)
        self.assertEqual(cmd, "cd /home/dsantacruz/projects/penguin && docker compose pull postgres && docker compose up -d postgres")
        app = container("app", 30, project="my_Portfolio_React_v2", files="docker-compose.yml", service="portfolio")
        cmd = julia.compose_update_cmd(app, CONFIG)
        self.assertIn("docker compose build --pull portfolio && docker compose up -d portfolio", cmd)
        self.assertIn("webhook", cmd)
        wazuh = container("wm", 30, has_build=False, project="wazuh-docker/single-node",
                          files="docker-compose.yml,docker-compose.override.yml", service="wazuh.manager")
        self.assertNotIn("-f", julia.compose_update_cmd(wazuh, CONFIG))
        sonar = {"name": "sonarqube", "image": "sonarqube:community", "compose_dir": None, "compose_service": None}
        self.assertEqual(julia.compose_update_cmd(sonar, CONFIG), "docker pull sonarqube:community y recrear sonarqube")

    def test_compose_service_has_build(self):
        with tempfile.TemporaryDirectory() as tmp:
            base, override = Path(tmp) / "docker-compose.yml", Path(tmp) / "docker-compose.override.yml"
            base.write_text("services:\n  app:\n    image: x\n  db:\n    image: postgres:16\n")
            override.write_text("services:\n  app:\n    build: .\n")
            files = f"{base},{override}"
            self.assertTrue(julia.compose_service_has_build(files, "app"))
            self.assertFalse(julia.compose_service_has_build(files, "db"))
            self.assertIsNone(julia.compose_service_has_build(f"{tmp}/missing.yml", "db"))

    def test_split_image_ref(self):
        self.assertEqual(julia.split_image_ref("wazuh/wazuh-manager:4.14.8"), ("wazuh", "wazuh-manager", "4.14.8"))
        self.assertEqual(julia.split_image_ref("postgres:16"), ("library", "postgres", "16"))
        self.assertEqual(julia.split_image_ref("client-gateway-prod"), ("library", "client-gateway-prod", "latest"))
        self.assertEqual(julia.split_image_ref("ghcr.io/org/app:1.2.3"), (None, None, "1.2.3"))
        self.assertEqual(julia.split_image_ref("docker.io/library/nats:2.10.1"), ("library", "nats", "2.10.1"))

    def test_pinned_and_mutable_images(self):
        inv = inventory(containers=[
            container("single-node-wazuh.manager-1", 60, image="wazuh/wazuh-manager:4.14.8", has_build=False,
                      project="wazuh-docker/single-node", files="docker-compose.yml,docker-compose.override.yml",
                      service="wazuh.manager"),
            container("single-node-wazuh.indexer-1", 60, image="wazuh/wazuh-indexer:4.14.8", has_build=False,
                      project="wazuh-docker/single-node", files="docker-compose.yml", service="wazuh.indexer"),
            container("penguin-postgres-1", 30, image="pgvector/pgvector:pg16", has_build=False, project="penguin",
                      files="docker-compose.yml", service="postgres"),
            container("daniel-portfolio-app", 30, image="my_portfolio_react_v2-portfolio", project="my_Portfolio_React_v2",
                      files="docker-compose.yml", service="portfolio"),
            container("fresh", 3, image="nats:alpine3.21", has_build=False),
        ])
        findings, ok = julia.image_findings(inv, CONFIG, self.lookup)
        by_key = {f["key"]: f for f in findings}
        self.assertEqual(sorted(by_key), ["image-tag:single-node-wazuh.manager-1", "image:daniel-portfolio-app",
                                          "image:penguin-postgres-1"])
        wazuh = by_key["image-tag:single-node-wazuh.manager-1"]
        self.assertIn("4.14.10", wazuh["title"])
        self.assertIn("docker compose pull wazuh.manager", wazuh["fix"])
        self.assertNotIn("webhook", wazuh["fix"])
        self.assertTrue(any("wazuh-indexer:4.14.8 (fijada, ultimo parche" in o for o in ok))
        pg = by_key["image:penguin-postgres-1"]
        self.assertIn("docker compose pull postgres", pg["fix"])
        self.assertNotIn("build --pull", pg["fix"])
        self.assertNotIn("webhook", pg["fix"])
        app = by_key["image:daniel-portfolio-app"]
        self.assertIn("build --pull portfolio", app["fix"])
        self.assertIn("webhook", app["fix"])
        # Solo se consulta Docker Hub para los tags fijados.
        self.assertEqual(sorted(c[1] for c in self.calls), ["wazuh-indexer", "wazuh-manager"])

    def test_newer_patch_tag(self):
        self.assertEqual(julia.newer_patch_tag("4.14.8", ["4.14.9", "4.15.0", "4.14.10", "5.0.0"]), "4.14.10")
        self.assertIsNone(julia.newer_patch_tag("4.14.8", ["4.14.8", "4.14.7", "4.15.1"]))
        self.assertEqual(julia.newer_patch_tag("1.27.3-alpine", ["1.27.4", "1.27.5-alpine"]), "1.27.5-alpine")
        self.assertIsNone(julia.newer_patch_tag("pg16", ["pg17"]))


class DedupAndOutputTests(unittest.TestCase):
    NOW = datetime(2026, 11, 8, 13, 50, tzinfo=timezone.utc)

    def findings(self):
        return [
            julia.finding("kev:CVE-1:kernel", "CRITICAL", "kev", "CVE-1 <script>", "a_b *c*", "sudo reboot", immediate=True),
            julia.finding("usn:USN-1", "HIGH", "patches", "USN-1", "x", "sudo apt install --only-upgrade x"),
            julia.finding("sysctl:mptcp", "LOW", "hardening", "MPTCP", "y", "z"),
        ]

    def test_dedup_cooldown_and_resolved(self):
        cooldown = timedelta(hours=24)
        alerts, comms, state, resolved = julia.plan_notifications(self.findings(), {}, self.NOW, cooldown)
        self.assertEqual([f["key"] for f in alerts], ["kev:CVE-1:kernel"])
        self.assertEqual([f["key"] for f in comms], ["kev:CVE-1:kernel", "usn:USN-1"])
        self.assertNotIn("sysctl:mptcp", state["open"])

        # Una hora despues: nada se repite.
        alerts, comms, state2, _ = julia.plan_notifications(self.findings(), state, self.NOW + timedelta(hours=1), cooldown)
        self.assertEqual((alerts, comms), ([], []))
        # Corrida del dia siguiente unos segundos "antes" de 24h: se repite igual.
        alerts, comms, _, _ = julia.plan_notifications(self.findings(), state, self.NOW + timedelta(hours=23, minutes=59), cooldown)
        self.assertEqual(len(alerts), 1)
        self.assertEqual(len(comms), 2)

        # El kernel se reinicio: queda resuelto y sale del estado.
        _, _, state3, resolved = julia.plan_notifications(self.findings()[1:], state2, self.NOW + timedelta(hours=2), cooldown)
        self.assertEqual([r["key"] for r in resolved], ["kev:CVE-1:kernel"])
        self.assertNotIn("kev:CVE-1:kernel", state3["open"])
        self.assertEqual(state3["open"]["usn:USN-1"]["first_seen"], self.NOW.isoformat())

    def test_message_escapes_and_fits(self):
        text = julia.build_report_message(self.findings(), ["host v22.23.4"], [], "2026-11-08", {"kev": "red"}, 8)
        self.assertIn("🕵️ <b>Julia — informe de inteligencia</b>", text)
        self.assertIn("CVE-1 &lt;script&gt;", text)
        self.assertNotIn("<script>", text)
        for _, title in julia.SECTIONS:
            self.assertIn(title, text)
        many = [julia.finding(f"usn:{i}", "HIGH", "patches", "x" * 200, "y" * 300, "z" * 100) for i in range(40)]
        text = julia.build_report_message(many, [], [], "2026-11-08", {}, 8)
        self.assertLessEqual(len(text), julia.TELEGRAM_LIMIT)
        self.assertIn("mas en el reporte", text)

    def test_comms_entry(self):
        entry = julia.comms_entry(self.findings()[0], self.NOW)
        self.assertEqual((entry["from"], entry["to"], entry["type"], entry["priority"], entry["read"]),
                         ("julia", "batman", "threat_intel", "critical", False))


class FeedCacheTests(unittest.TestCase):
    def test_once_per_run_cache_and_fallback(self):
        calls = []

        def fetch(url, timeout):
            calls.append(url)
            if "down" in url:
                raise urllib.error.URLError("sin red")
            if "missing" in url:
                raise julia.FeedNotFound(url)
            return {"url": url}

        with tempfile.TemporaryDirectory() as tmp:
            cache = julia.FeedCache(Path(tmp), 5, max_cve_lookups=1, fetch=fetch)
            age = timedelta(hours=20)
            self.assertEqual(cache.get("kev", "https://a/kev", age), {"url": "https://a/kev"})
            cache.get("kev", "https://a/kev", age)
            self.assertEqual(len(calls), 1)
            self.assertEqual(cache.status["kev"], "red")

            # Corrida nueva: cache fresco, no va a la red.
            cache2 = julia.FeedCache(Path(tmp), 5, max_cve_lookups=1, fetch=fetch)
            cache2.get("kev", "https://a/kev", age)
            self.assertEqual((len(calls), cache2.status["kev"]), (1, "cache"))

            # Cache vencido + red caida: usa el viejo y lo dice.
            cache3 = julia.FeedCache(Path(tmp), 5, max_cve_lookups=1, fetch=fetch)
            self.assertEqual(cache3.get("kev", "https://down/kev", timedelta(0)), {"url": "https://a/kev"})
            self.assertTrue(cache3.status["kev"].startswith("cache viejo"))

            # 404 se cachea como None; el tope de CVEs corta la siguiente.
            self.assertIsNone(cache3.get("cves/CVE-1", "https://missing/1", age, is_cve=True))
            self.assertEqual(cache3.status["cves/CVE-1"], "404")
            self.assertIsNone(cache3.get("cves/CVE-2", "https://a/2", age, is_cve=True))
            self.assertIn("tope", cache3.status["cves/CVE-2"])
            self.assertNotIn("https://a/2", calls)


if __name__ == "__main__":
    unittest.main()
