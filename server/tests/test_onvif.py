"""ONVIF services, authentication, discovery and the HTTP endpoint, with the
cameras faked (tests for the real ioctls run on the Jetson only)."""

from __future__ import annotations

import base64
import hashlib
import threading
import time
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

import pytest

from skydome_server.camera_settings import FALLBACK_CONTROLS, CameraSettings
from skydome_server.net import Interface, parse_ip_json
from skydome_server.onvif.discovery import Discovery
from skydome_server.onvif.http import OnvifServer, SnapshotSource, load_credentials
from skydome_server.onvif.services import Identity, Request, Services, Stream, _posix_tz
from skydome_server.onvif.soap import NS, Fault, UsernameTokenAuth, envelope, fault_envelope, parse
from skydome_server.v4l2 import CameraNode, control_name, find_cameras

USER, PASSWORD = "admin", "s3cret"
SOAP = "http://www.w3.org/2003/05/soap-envelope"


class FakeDevice:
    log: list = []

    def __init__(self, path):
        self.path = path

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass

    def controls(self):
        return dict(FALLBACK_CONTROLS)

    def set(self, cid, value):
        name = next(c.name for c in FALLBACK_CONTROLS.values() if c.id == cid)
        FakeDevice.log.append((self.path, name, value))


@pytest.fixture
def cams():
    FakeDevice.log = []
    return [CameraNode("/dev/video0", ("video0", "2-1.1", "3")), CameraNode("/dev/video2", ("video2", "2-1.2", "4"))]


@pytest.fixture
def settings(tmp_path, cams):
    return CameraSettings(tmp_path / "camera_settings.yaml", finder=lambda: list(cams), opener=FakeDevice,
                          log=lambda *a, **k: None)


@pytest.fixture
def services(settings):
    ident = Identity(uuid="urn:uuid:11111111-2222-3333-4444-555555555555", hostname="skydome", firmware="abc123",
                     serial="1422", location="Surprise, AZ", timezone="America/Phoenix")
    nets = [Interface("wlan0", "aa:bb:cc:dd:ee:ff", 1500, "10.0.0.5", 24, True)]
    return Services(ident, Stream(4096, 15, 20000, "dome"), settings, {USER: PASSWORD},
                    interfaces=lambda: nets, log=lambda *a, **k: None)


def request(body: str, header: str = "") -> bytes:
    ns = " ".join(f'xmlns:{p}="{u}"' for p, u in NS.items())
    return (f'<s:Envelope {ns}><s:Header>{header}</s:Header><s:Body>{body}</s:Body></s:Envelope>').encode()


def call(services: Services, body: str) -> ET.Element:
    """Run one operation; returns the response element (well-formedness checked)."""
    _, op = parse(request(body))
    out = ET.fromstring(envelope(services.handle(Request(op, "10.0.0.5", 80, USER))))
    return out.find("s:Body", NS)[0]


def token(user=USER, password=PASSWORD, nonce=b"0123456789abcdef", created=None, digest=True) -> str:
    created = created or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if digest:
        pw = base64.b64encode(hashlib.sha1(nonce + created.encode() + password.encode()).digest()).decode()
        ptype = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0#PasswordDigest"
    else:
        pw, ptype = password, "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0#PasswordText"
    return (f"<wsse:Security><wsse:UsernameToken><wsse:Username>{user}</wsse:Username>"
            f'<wsse:Password Type="{ptype}">{pw}</wsse:Password>'
            f"<wsse:Nonce>{base64.b64encode(nonce).decode()}</wsse:Nonce><wsu:Created>{created}</wsu:Created>"
            "</wsse:UsernameToken></wsse:Security>")


def header(tok: str) -> ET.Element:
    h, _ = parse(request("<tds:GetDeviceInformation/>", tok))
    return h


# ---- Media -----------------------------------------------------------------------

def test_profile_and_stream_uri(services):
    resp = call(services, "<trt:GetProfiles/>")
    prof = resp.find("trt:Profiles", NS)
    assert prof.get("token") == "dome"
    assert prof.find("tt:VideoEncoderConfiguration/tt:Resolution/tt:Width", NS).text == "4096"
    assert prof.find("tt:VideoEncoderConfiguration/tt:RateControl/tt:FrameRateLimit", NS).text == "15"
    uri = call(services, "<trt:GetStreamUri><trt:StreamSetup/><trt:ProfileToken>dome</trt:ProfileToken></trt:GetStreamUri>")
    assert uri.find("trt:MediaUri/tt:Uri", NS).text == "rtsp://10.0.0.5:8554/dome"
    snap = call(services, "<trt:GetSnapshotUri><trt:ProfileToken>dome</trt:ProfileToken></trt:GetSnapshotUri>")
    assert snap.find("trt:MediaUri/tt:Uri", NS).text == "http://10.0.0.5:80/onvif/snapshot.jpg"


def test_unknown_profile_is_a_fault(services):
    with pytest.raises(Fault) as e:
        call(services, "<trt:GetStreamUri><trt:ProfileToken>other</trt:ProfileToken></trt:GetStreamUri>")
    assert e.value.subcodes == ["ter:InvalidArgVal", "ter:NoProfile"] and e.value.status == 400


def test_encoder_config_is_fixed(services):
    same = ("<trt:SetVideoEncoderConfiguration><trt:Configuration token=\"dome_h264\"><tt:Encoding>H264</tt:Encoding>"
            "<tt:Resolution><tt:Width>4096</tt:Width><tt:Height>4096</tt:Height></tt:Resolution>"
            "<tt:RateControl><tt:FrameRateLimit>15</tt:FrameRateLimit><tt:BitrateLimit>8000</tt:BitrateLimit></tt:RateControl>"
            "</trt:Configuration><trt:ForcePersistence>true</trt:ForcePersistence></trt:SetVideoEncoderConfiguration>")
    call(services, same)   # bitrate is ignored, the shape matches
    with pytest.raises(Fault) as e:
        call(services, same.replace("<tt:Width>4096", "<tt:Width>1920"))
    assert e.value.subcodes[-1] == "ter:ConfigModify"


def test_no_audio_and_unsupported(services):
    assert len(call(services, "<trt:GetAudioSources/>")) == 0
    with pytest.raises(Fault) as e:
        call(services, "<tds:SystemReboot/>")
    assert e.value.subcodes == ["ter:ActionNotSupported"] and e.value.status == 500


# ---- Device ----------------------------------------------------------------------

def test_capabilities_and_services(services):
    caps = call(services, "<tds:GetCapabilities><tds:Category>All</tds:Category></tds:GetCapabilities>")
    c = caps.find("tds:Capabilities", NS)
    assert c.find("tt:Media/tt:XAddr", NS).text == "http://10.0.0.5:80/onvif/media_service"
    assert c.find("tt:Imaging/tt:XAddr", NS).text == "http://10.0.0.5:80/onvif/imaging_service"
    assert c.find("tt:PTZ", NS) is None
    with pytest.raises(Fault):
        call(services, "<tds:GetCapabilities><tds:Category>PTZ</tds:Category></tds:GetCapabilities>")
    svcs = call(services, "<tds:GetServices><tds:IncludeCapability>true</tds:IncludeCapability></tds:GetServices>")
    names = [s.find("tds:Namespace", NS).text for s in svcs.findall("tds:Service", NS)]
    assert names == [NS["tds"], NS["trt"], NS["timg"]]
    assert svcs.find("tds:Service/tds:Capabilities/tds:Capabilities/tds:Security", NS).get("UsernameToken") == "true"


def test_device_information_scopes_network(services):
    info = call(services, "<tds:GetDeviceInformation/>")
    assert info.find("tds:FirmwareVersion", NS).text == "abc123"
    scopes = [s.text for s in call(services, "<tds:GetScopes/>").findall("tds:Scopes/tt:ScopeItem", NS)]
    assert "onvif://www.onvif.org/location/city/Surprise%2C%20AZ" in scopes
    net = call(services, "<tds:GetNetworkInterfaces/>").find("tds:NetworkInterfaces", NS)
    assert net.find("tt:IPv4/tt:Config/tt:FromDHCP/tt:Address", NS).text == "10.0.0.5"


def test_date_and_time(services):
    dt = call(services, "<tds:GetSystemDateAndTime/>").find("tds:SystemDateAndTime", NS)
    assert dt.find("tt:DateTimeType", NS).text == "NTP"
    year = int(dt.find("tt:UTCDateTime/tt:Date/tt:Year", NS).text)
    assert year == datetime.now(timezone.utc).year


def test_posix_tz_from_tzif_footer(tmp_path):
    (tmp_path / "America").mkdir()
    (tmp_path / "America" / "Phoenix").write_bytes(b"TZif2" + b"\0" * 40 + b"\nMST7\n")
    assert _posix_tz("America/Phoenix", tmp_path) == "MST7"
    assert _posix_tz("Nowhere/Town", tmp_path) is None


# ---- Imaging ---------------------------------------------------------------------

IMG = "<timg:VideoSourceToken>dome_src</timg:VideoSourceToken>"


def test_imaging_settings_and_options(services):
    s = call(services, f"<timg:GetImagingSettings>{IMG}</timg:GetImagingSettings>").find("timg:ImagingSettings", NS)
    assert s.find("tt:Brightness", NS).text == "128"
    assert s.find("tt:Exposure/tt:Mode", NS).text == "AUTO"
    assert s.find("tt:Exposure/tt:ExposureTime", NS).text == "15600"   # 156 × 100 µs
    assert s.find("tt:BacklightCompensation/tt:Mode", NS).text == "ON"
    o = call(services, f"<timg:GetOptions>{IMG}</timg:GetOptions>").find("timg:ImagingOptions", NS)
    assert o.find("tt:Exposure/tt:ExposureTime/tt:Max", NS).text == "1000000"   # 1 s
    assert o.find("tt:Exposure/tt:Gain/tt:Max", NS).text == "190"
    assert o.find("tt:Sharpness/tt:Min", NS).text == "14"
    with pytest.raises(Fault) as e:
        call(services, "<timg:GetImagingSettings><timg:VideoSourceToken>x</timg:VideoSourceToken></timg:GetImagingSettings>")
    assert e.value.subcodes[-1] == "ter:NoSource"


def test_set_manual_exposure_applies_to_every_camera(services, settings, cams):
    call(services, f"<timg:SetImagingSettings>{IMG}<timg:ImagingSettings>"
                   "<tt:Exposure><tt:Mode>MANUAL</tt:Mode><tt:ExposureTime>20000</tt:ExposureTime><tt:Gain>100</tt:Gain></tt:Exposure>"
                   "<tt:WhiteBalance><tt:Mode>MANUAL</tt:Mode></tt:WhiteBalance>"
                   "<tt:IrCutFilter>OFF</tt:IrCutFilter>"
                   "</timg:ImagingSettings></timg:SetImagingSettings>")
    for cam in cams:
        sets = [(n, v) for d, n, v in FakeDevice.log if d == cam.device]
        names = [n for n, _ in sets]
        # automatic modes off before the manual values, which a UVC camera refuses otherwise
        assert names.index("auto_exposure") < names.index("exposure_time_absolute")
        assert ("exposure_time_absolute", 200) in sets and ("gain", 100) in sets
        assert ("white_balance_automatic", 0) in sets
    assert "exposure_time_absolute: 200" in settings.path.read_text()
    s = call(services, f"<timg:GetImagingSettings>{IMG}</timg:GetImagingSettings>").find("timg:ImagingSettings", NS)
    assert s.find("tt:Exposure/tt:Mode", NS).text == "MANUAL"
    assert s.find("tt:Exposure/tt:ExposureTime", NS).text == "20000"


def test_set_echo_is_a_no_op_and_ranges_are_checked(services, settings):
    current = call(services, f"<timg:GetImagingSettings>{IMG}</timg:GetImagingSettings>").find("timg:ImagingSettings", NS)
    echoed = ET.tostring(current, encoding="unicode")
    call(services, f"<timg:SetImagingSettings>{IMG}{echoed}</timg:SetImagingSettings>")
    assert FakeDevice.log == [] and not settings.path.exists()
    with pytest.raises(Fault) as e:
        call(services, f"<timg:SetImagingSettings>{IMG}<timg:ImagingSettings><tt:Brightness>300</tt:Brightness>"
                       "</timg:ImagingSettings></timg:SetImagingSettings>")
    assert e.value.subcodes[-1] == "ter:SettingsInvalid"


def test_backlight_off_and_on(services, settings):
    call(services, f"<timg:SetImagingSettings>{IMG}<timg:ImagingSettings><tt:BacklightCompensation>"
                   "<tt:Mode>OFF</tt:Mode></tt:BacklightCompensation></timg:ImagingSettings></timg:SetImagingSettings>")
    assert settings.values["backlight_compensation"] == 0
    call(services, f"<timg:SetImagingSettings>{IMG}<timg:ImagingSettings><tt:BacklightCompensation>"
                   "<tt:Mode>ON</tt:Mode></tt:BacklightCompensation></timg:ImagingSettings></timg:SetImagingSettings>")
    assert settings.values["backlight_compensation"] == 48


# ---- camera settings store -------------------------------------------------------

def test_replugged_camera_gets_the_settings_again(settings, cams):
    settings.update({"brightness": 90, "exposure_time_absolute": 300})   # exposure stays auto: not sent
    assert ("/dev/video0", "brightness", 90) in FakeDevice.log
    assert not any(n == "exposure_time_absolute" for _, n, _ in FakeDevice.log)
    FakeDevice.log.clear()
    settings.sync()
    assert FakeDevice.log == []        # nothing new
    cams[1] = CameraNode("/dev/video4", ("video4", "2-1.2", "9"))   # re-enumerated
    settings.sync()
    assert FakeDevice.log == [("/dev/video4", "brightness", 90)]
    again = CameraSettings(settings.path, finder=lambda: [], opener=FakeDevice)
    assert again.values == {"brightness": 90, "exposure_time_absolute": 300}


def test_find_cameras_by_usb_id(tmp_path):
    usb = tmp_path / "devices"
    for i, (vid, pid, index) in enumerate([("32e4", "6678", "0"), ("32e4", "6678", "1"), ("046d", "0825", "0")]):
        dev = usb / f"2-1.{i}"
        (dev / f"2-1.{i}:1.0").mkdir(parents=True)
        (dev / "idVendor").write_text(vid)
        (dev / "idProduct").write_text(pid)
        (dev / "devnum").write_text(str(10 + i))
        node = tmp_path / "class" / f"video{i * 2}"
        node.mkdir(parents=True)
        (node / "index").write_text(index)
        (node / "device").symlink_to(dev / f"2-1.{i}:1.0")
    found = find_cameras(tmp_path / "class")
    assert [c.device for c in found] == ["/dev/video0"]
    assert found[0].key == ("video0", "2-1.0", "10")


def test_control_names_match_v4l2_ctl():
    assert control_name(b"Exposure Time, Absolute\0junk") == "exposure_time_absolute"
    assert control_name(b"White Balance, Automatic") == "white_balance_automatic"


# ---- authentication --------------------------------------------------------------

def test_digest_auth():
    auth = UsernameTokenAuth({USER: PASSWORD})
    assert auth.check(header(token())) == USER
    with pytest.raises(Fault) as e:
        auth.check(header(token()))                       # same nonce again
    assert e.value.subcodes == ["ter:NotAuthorized"]
    with pytest.raises(Fault):
        auth.check(header(token(password="wrong", nonce=b"another-nonce-01")))
    with pytest.raises(Fault):
        auth.check(header(token(nonce=b"another-nonce-02", created="2001-01-01T00:00:00Z")))
    with pytest.raises(Fault):
        auth.check(None)
    assert auth.check(header(token(digest=False))) == USER


def test_pre_auth_operations(services):
    _, op = parse(request("<tds:GetSystemDateAndTime/>"))
    assert not services.needs_auth(op)
    _, op = parse(request("<trt:GetProfiles/>"))
    assert services.needs_auth(op)


def test_parse_rejects_dtds_and_junk():
    with pytest.raises(Fault):
        parse(b'<!DOCTYPE x [<!ENTITY a "b">]><x/>')
    with pytest.raises(Fault):
        parse(b"not xml")
    f = Fault("Sender", ["ter:InvalidArgVal", "ter:NoProfile"], "x")
    code = ET.fromstring(fault_envelope(f)).find("s:Body/s:Fault/s:Code", NS)
    assert code.find("s:Subcode/s:Subcode/s:Value", NS).text == "ter:NoProfile"


# ---- discovery -------------------------------------------------------------------

def probe(types: str, extra_ns: str = 'xmlns:dn="http://www.onvif.org/ver10/network/wsdl"', scopes: str = "") -> bytes:
    return (f'<e:Envelope xmlns:e="{SOAP}" xmlns:w="http://schemas.xmlsoap.org/ws/2004/08/addressing" '
            f'xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery" {extra_ns}>'
            "<e:Header><w:MessageID>uuid:probe-1</w:MessageID></e:Header>"
            f"<e:Body><d:Probe><d:Types>{types}</d:Types><d:Scopes>{scopes}</d:Scopes></d:Probe></e:Body></e:Envelope>").encode()


@pytest.fixture
def disc():
    return Discovery("urn:uuid:abc", ["onvif://www.onvif.org/name/SkyDome", "onvif://www.onvif.org/type/video_encoder"],
                     lambda ip: f"http://{ip}:80/onvif/device_service", local_for=lambda peer: "10.0.0.5")


def test_probe_match(disc):
    reply = disc.handle(probe("dn:NetworkVideoTransmitter"), "10.0.0.9")
    root = ET.fromstring(reply)
    d = "{http://schemas.xmlsoap.org/ws/2005/04/discovery}"
    a = "{http://schemas.xmlsoap.org/ws/2004/08/addressing}"
    assert root.find(f".//{a}RelatesTo").text == "uuid:probe-1"
    assert root.find(f".//{d}XAddrs").text == "http://10.0.0.5:80/onvif/device_service"
    assert root.find(f".//{a}Address").text == "urn:uuid:abc"
    assert disc.handle(probe(""), "10.0.0.9") is not None                     # probe for anything
    assert disc.handle(probe("dn:NetworkVideoTransmitter", scopes="onvif://www.onvif.org/name"), "x") is not None


def test_probe_mismatch(disc):
    # Windows asks for printers and PCs as wsdp:Device all day: not us
    assert disc.handle(probe("wsdp:Device", 'xmlns:wsdp="http://schemas.xmlsoap.org/ws/2006/02/devprof"'), "x") is None
    assert disc.handle(probe("dn:NetworkVideoTransmitter", scopes="onvif://www.onvif.org/name/Other"), "x") is None
    assert disc.handle(b"garbage", "x") is None
    assert disc.handle(disc.hello("10.0.0.5"), "x") is None                    # our own announcement


# ---- HTTP end to end -------------------------------------------------------------

def test_http_endpoint(services, tmp_path):
    snap = tmp_path / "snapshot.jpg"
    server = OnvifServer(("127.0.0.1", 0), services, UsernameTokenAuth({USER: PASSWORD}), SnapshotSource(snap, timeout=2))
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    stop = threading.Event()

    def fake_live():   # skydome-live's side of the snapshot handshake
        req = snap.with_name(snap.name + ".request")
        while not stop.is_set():
            if req.exists():
                req.unlink()
                snap.write_bytes(b"\xff\xd8 dome \xff\xd9")
            time.sleep(0.01)

    threading.Thread(target=fake_live, daemon=True).start()
    try:
        def post(body, tok=""):
            r = urllib.request.Request(f"http://127.0.0.1:{port}/onvif/device_service", data=request(body, tok),
                                       headers={"Content-Type": "application/soap+xml"})
            try:
                with urllib.request.urlopen(r, timeout=5) as resp:
                    return resp.status, resp.read()
            except urllib.error.HTTPError as e:
                return e.code, e.read()

        status, _ = post("<tds:GetDeviceInformation/>")
        assert status == 400                                       # no credentials
        status, body = post("<tds:GetDeviceInformation/>", token())
        assert status == 200 and b"abc123" in body
        status, body = post("<tds:GetCapabilities/>")
        assert status == 200 and f"127.0.0.1:{port}/onvif/media_service".encode() in body
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/onvif/snapshot.jpg", timeout=5) as resp:
            assert resp.headers["Content-Type"] == "image/jpeg" and resp.read().startswith(b"\xff\xd8")
    finally:
        stop.set()
        server.shutdown()
        server.server_close()


def test_snapshot_without_skydome_live(tmp_path):
    src = SnapshotSource(tmp_path / "snapshot.jpg", timeout=0.2)
    assert src.get() is None
    assert not (tmp_path / "snapshot.jpg.request").exists()


def test_credentials_created_private(tmp_path):
    path = tmp_path / "onvif_credentials.yaml"
    users = load_credentials(path)
    assert list(users) == ["admin"] and len(users["admin"]) >= 12
    assert path.stat().st_mode & 0o777 == 0o600
    assert load_credentials(path) == users


def test_ip_json():
    text = ('[{"ifname":"lo","flags":["LOOPBACK","UP","LOWER_UP"],"addr_info":[{"family":"inet","local":"127.0.0.1","prefixlen":8}]},'
            '{"ifname":"wlP1p1s0","flags":["UP","LOWER_UP"],"mtu":1500,"address":"aa:bb:cc:dd:ee:ff",'
            '"addr_info":[{"family":"inet","local":"192.0.2.81","prefixlen":24,"dynamic":true}]},'
            '{"ifname":"l4tbr0","flags":["NO-CARRIER","UP"],"addr_info":[{"family":"inet","local":"192.168.55.1","prefixlen":24}]}]')
    assert parse_ip_json(text) == [Interface("wlP1p1s0", "aa:bb:cc:dd:ee:ff", 1500, "192.0.2.81", 24, True)]
