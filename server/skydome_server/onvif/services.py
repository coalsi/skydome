"""ONVIF Profile S for the dome: Device (ver10), Media (ver10), Imaging (ver20).

One video source (the stitched dome), one fixed profile (the Detail stream,
D-014/D-015) and imaging settings that act on all five cameras at once.
Anything ONVIF can ask for that doesn't apply to this rig (PTZ, audio, I/O,
events, profile editing) is answered with the standard fault or an empty
list, which NVRs take as "this camera doesn't have it".
"""

from __future__ import annotations

import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from urllib.parse import quote
from xml.sax.saxutils import escape
from zoneinfo import ZoneInfo

from ..camera_settings import CameraSettings
from ..net import Interface
from .soap import NS, Fault, child_text, invalid, not_supported, split_tag, xml_bool

PROFILE = "dome"
SOURCE = "dome_src"
SOURCE_CFG = "dome_src_cfg"
ENCODER = "dome_h264"
ONVIF_VERSION = (2, 60)

# Answered without credentials (ONVIF Core §5.9.4, "PRE_AUTH")
PRE_AUTH = {"GetSystemDateAndTime", "GetCapabilities", "GetServices", "GetServiceCapabilities",
            "GetWsdlUrl", "GetHostname", "GetEndpointReference"}

# ONVIF name -> V4L2 control, for the plain numeric imaging settings
_LEVELS = (("Brightness", "brightness"), ("ColorSaturation", "saturation"), ("Contrast", "contrast"))


def _exposure_map(settings) -> tuple[str, int, int, int]:
    """(exposure control, µs per unit, automatic, manual) for these settings:
    the UVC cameras' or the ZWO camera's (D-029)."""
    return getattr(settings, "ONVIF_EXPOSURE", CameraSettings.ONVIF_EXPOSURE)


@dataclass
class Identity:
    uuid: str                      # urn:uuid:... for discovery
    hostname: str
    firmware: str
    serial: str
    name: str = "SkyDome"
    location: str = ""
    timezone: str = "UTC"
    manufacturer: str = "SkyDome"
    model: str = "SkyDome all-sky camera"
    hardware: str = "Jetson Orin Nano Super, 5x U3CAM4K60"

    def scopes(self) -> list[str]:
        s = ["onvif://www.onvif.org/type/video_encoder", "onvif://www.onvif.org/type/NetworkVideoTransmitter",
             "onvif://www.onvif.org/Profile/Streaming",
             f"onvif://www.onvif.org/hardware/{quote(self.model)}", f"onvif://www.onvif.org/name/{quote(self.name)}"]
        if self.location:
            s.append(f"onvif://www.onvif.org/location/city/{quote(self.location)}")
        return s


@dataclass
class Stream:
    size: int
    fps: int
    bitrate_kbps: int
    path: str
    rtsp_port: int = 8554

    @property
    def gov(self) -> int:
        return self.fps * 2   # key-int-max in skydome-live's encoder


@dataclass
class Request:
    op: ET.Element
    host: str                       # our address as the client reached it
    port: int
    user: str | None = None

    @property
    def base(self) -> str:
        return f"http://{self.host}:{self.port}"


@dataclass
class Services:
    identity: Identity
    stream: Stream
    settings: CameraSettings
    users: dict[str, str]
    interfaces: Callable[[], list[Interface]] = list
    log: Callable[..., None] = print
    handlers: dict = field(init=False)

    def __post_init__(self):
        d, m, i = NS["tds"], NS["trt"], NS["timg"]
        self.handlers = {
            (d, "GetDeviceInformation"): self.device_information,
            (d, "GetCapabilities"): self.capabilities,
            (d, "GetServices"): self.services,
            (d, "GetServiceCapabilities"): lambda r: f"<tds:GetServiceCapabilitiesResponse>{self._device_caps()}</tds:GetServiceCapabilitiesResponse>",
            (d, "GetSystemDateAndTime"): self.system_date_and_time,
            (d, "SetSystemDateAndTime"): self.set_system_date_and_time,
            (d, "GetScopes"): self.scopes,
            (d, "GetHostname"): lambda r: f"<tds:GetHostnameResponse><tds:HostnameInformation><tt:FromDHCP>false</tt:FromDHCP><tt:Name>{escape(self.identity.hostname)}</tt:Name></tds:HostnameInformation></tds:GetHostnameResponse>",
            (d, "GetNetworkInterfaces"): self.network_interfaces,
            (d, "GetNetworkProtocols"): self.network_protocols,
            (d, "GetDNS"): lambda r: "<tds:GetDNSResponse><tds:DNSInformation><tt:FromDHCP>true</tt:FromDHCP></tds:DNSInformation></tds:GetDNSResponse>",
            (d, "GetNTP"): lambda r: "<tds:GetNTPResponse><tds:NTPInformation><tt:FromDHCP>false</tt:FromDHCP></tds:NTPInformation></tds:GetNTPResponse>",
            (d, "GetDiscoveryMode"): lambda r: "<tds:GetDiscoveryModeResponse><tds:DiscoveryMode>Discoverable</tds:DiscoveryMode></tds:GetDiscoveryModeResponse>",
            (d, "GetWsdlUrl"): lambda r: "<tds:GetWsdlUrlResponse><tds:WsdlUrl>http://www.onvif.org/</tds:WsdlUrl></tds:GetWsdlUrlResponse>",
            (d, "GetEndpointReference"): lambda r: f"<tds:GetEndpointReferenceResponse><tds:GUID>{self.identity.uuid}</tds:GUID></tds:GetEndpointReferenceResponse>",
            (d, "GetUsers"): self.get_users,
            (d, "GetRelayOutputs"): lambda r: "<tds:GetRelayOutputsResponse/>",
            (m, "GetServiceCapabilities"): lambda r: f"<trt:GetServiceCapabilitiesResponse>{self._media_caps()}</trt:GetServiceCapabilitiesResponse>",
            (m, "GetProfiles"): lambda r: f"<trt:GetProfilesResponse>{self._profile('trt:Profiles')}</trt:GetProfilesResponse>",
            (m, "GetProfile"): self.get_profile,
            (m, "GetVideoSources"): self.video_sources,
            (m, "GetVideoSourceConfigurations"): lambda r: f"<trt:GetVideoSourceConfigurationsResponse>{self._source_cfg('trt:Configurations')}</trt:GetVideoSourceConfigurationsResponse>",
            (m, "GetVideoSourceConfiguration"): self.video_source_configuration,
            (m, "GetCompatibleVideoSourceConfigurations"): self.compatible_source_cfgs,
            (m, "GetVideoSourceConfigurationOptions"): self.video_source_configuration_options,
            (m, "GetVideoEncoderConfigurations"): lambda r: f"<trt:GetVideoEncoderConfigurationsResponse>{self._encoder_cfg('trt:Configurations')}</trt:GetVideoEncoderConfigurationsResponse>",
            (m, "GetVideoEncoderConfiguration"): self.video_encoder_configuration,
            (m, "GetCompatibleVideoEncoderConfigurations"): self.compatible_encoder_cfgs,
            (m, "GetVideoEncoderConfigurationOptions"): self.video_encoder_configuration_options,
            (m, "SetVideoEncoderConfiguration"): self.set_video_encoder_configuration,
            (m, "GetGuaranteedNumberOfVideoEncoderInstances"): lambda r: "<trt:GetGuaranteedNumberOfVideoEncoderInstancesResponse><trt:TotalNumber>1</trt:TotalNumber><trt:H264>1</trt:H264></trt:GetGuaranteedNumberOfVideoEncoderInstancesResponse>",
            (m, "GetStreamUri"): self.stream_uri,
            (m, "GetSnapshotUri"): self.snapshot_uri,
            (m, "SetSynchronizationPoint"): lambda r: "<trt:SetSynchronizationPointResponse/>",
            (m, "CreateProfile"): lambda r: _raise(Fault("Receiver", ["ter:Action", "ter:MaxNVTProfiles"], "the dome has one fixed profile")),
            (m, "DeleteProfile"): lambda r: _raise(Fault("Sender", ["ter:Action", "ter:DeletionOfFixedProfile"], "the dome's profile is fixed")),
            (i, "GetServiceCapabilities"): lambda r: "<timg:GetServiceCapabilitiesResponse><timg:Capabilities ImageStabilization=\"false\" Presets=\"false\" AdaptablePreset=\"false\"/></timg:GetServiceCapabilitiesResponse>",
            (i, "GetImagingSettings"): self.imaging_settings,
            (i, "SetImagingSettings"): self.set_imaging_settings,
            (i, "GetOptions"): self.imaging_options,
            (i, "GetMoveOptions"): self._with_source(lambda r: "<timg:GetMoveOptionsResponse><timg:MoveOptions/></timg:GetMoveOptionsResponse>"),
            (i, "GetStatus"): self._with_source(lambda r: "<timg:GetStatusResponse><timg:Status/></timg:GetStatusResponse>"),
            (i, "Stop"): self._with_source(lambda r: "<timg:StopResponse/>"),
        }
        # Empty lists: the rig has no audio, metadata or analytics
        for op in ("GetAudioSources", "GetAudioOutputs", "GetAudioSourceConfigurations",
                   "GetAudioEncoderConfigurations", "GetAudioOutputConfigurations",
                   "GetMetadataConfigurations", "GetVideoAnalyticsConfigurations"):
            self.handlers[(m, op)] = lambda r, op=op: f"<trt:{op}Response/>"

    # ---- dispatch ----------------------------------------------------------------

    def handle(self, req: Request) -> str:
        ns, op = split_tag(req.op)
        handler = self.handlers.get((ns, op))
        if handler is None:
            raise not_supported(op)
        return handler(req)

    @staticmethod
    def needs_auth(op: ET.Element) -> bool:
        return split_tag(op)[1] not in PRE_AUTH

    # ---- Device ------------------------------------------------------------------

    def device_information(self, r: Request) -> str:
        i = self.identity
        return ("<tds:GetDeviceInformationResponse>"
                f"<tds:Manufacturer>{escape(i.manufacturer)}</tds:Manufacturer><tds:Model>{escape(i.model)}</tds:Model>"
                f"<tds:FirmwareVersion>{escape(i.firmware)}</tds:FirmwareVersion>"
                f"<tds:SerialNumber>{escape(i.serial)}</tds:SerialNumber><tds:HardwareId>{escape(i.hardware)}</tds:HardwareId>"
                "</tds:GetDeviceInformationResponse>")

    def capabilities(self, r: Request) -> str:
        wanted = {c.text.strip() for c in r.op.findall("tds:Category", NS) if c.text} or {"All"}
        everything = "All" in wanted
        parts = []
        if everything or "Device" in wanted:
            v = ONVIF_VERSION
            parts.append(
                f"<tt:Device><tt:XAddr>{r.base}/onvif/device_service</tt:XAddr>"
                "<tt:Network><tt:IPFilter>false</tt:IPFilter><tt:ZeroConfiguration>false</tt:ZeroConfiguration>"
                "<tt:IPVersion6>false</tt:IPVersion6><tt:DynDNS>false</tt:DynDNS></tt:Network>"
                "<tt:System><tt:DiscoveryResolve>true</tt:DiscoveryResolve><tt:DiscoveryBye>true</tt:DiscoveryBye>"
                "<tt:RemoteDiscovery>false</tt:RemoteDiscovery><tt:SystemBackup>false</tt:SystemBackup>"
                "<tt:SystemLogging>false</tt:SystemLogging><tt:FirmwareUpgrade>false</tt:FirmwareUpgrade>"
                f"<tt:SupportedVersions><tt:Major>{v[0]}</tt:Major><tt:Minor>{v[1]}</tt:Minor></tt:SupportedVersions>"
                "</tt:System>"
                "<tt:IO><tt:InputConnectors>0</tt:InputConnectors><tt:RelayOutputs>0</tt:RelayOutputs></tt:IO>"
                "<tt:Security><tt:TLS1.1>false</tt:TLS1.1><tt:TLS1.2>false</tt:TLS1.2>"
                "<tt:OnboardKeyGeneration>false</tt:OnboardKeyGeneration><tt:AccessPolicyConfig>false</tt:AccessPolicyConfig>"
                "<tt:X.509Token>false</tt:X.509Token><tt:SAMLToken>false</tt:SAMLToken>"
                "<tt:KerberosToken>false</tt:KerberosToken><tt:RELToken>false</tt:RELToken></tt:Security>"
                "</tt:Device>")
        if everything or "Imaging" in wanted:
            parts.append(f"<tt:Imaging><tt:XAddr>{r.base}/onvif/imaging_service</tt:XAddr></tt:Imaging>")
        if everything or "Media" in wanted:
            parts.append(f"<tt:Media><tt:XAddr>{r.base}/onvif/media_service</tt:XAddr>"
                         "<tt:StreamingCapabilities><tt:RTPMulticast>false</tt:RTPMulticast><tt:RTP_TCP>true</tt:RTP_TCP>"
                         "<tt:RTP_RTSP_TCP>true</tt:RTP_RTSP_TCP></tt:StreamingCapabilities></tt:Media>")
        if not parts:
            raise Fault("Receiver", ["ter:ActionNotSupported", "ter:NoSuchService"],
                        f"no {', '.join(sorted(wanted))} service on this device")
        return f"<tds:GetCapabilitiesResponse><tds:Capabilities>{''.join(parts)}</tds:Capabilities></tds:GetCapabilitiesResponse>"

    def services(self, r: Request) -> str:
        with_caps = xml_bool(child_text(r.op, "tds:IncludeCapability"))
        v = ONVIF_VERSION
        out = []
        for ns, path, caps in ((NS["tds"], "device_service", self._device_caps()),
                               (NS["trt"], "media_service", self._media_caps()),
                               (NS["timg"], "imaging_service",
                                '<timg:Capabilities ImageStabilization="false" Presets="false" AdaptablePreset="false"/>')):
            out.append(f"<tds:Service><tds:Namespace>{ns}</tds:Namespace><tds:XAddr>{r.base}/onvif/{path}</tds:XAddr>"
                       + (f"<tds:Capabilities>{caps}</tds:Capabilities>" if with_caps else "")
                       + f"<tds:Version><tt:Major>{v[0]}</tt:Major><tt:Minor>{v[1]}</tt:Minor></tds:Version></tds:Service>")
        return f"<tds:GetServicesResponse>{''.join(out)}</tds:GetServicesResponse>"

    def _device_caps(self) -> str:
        return ('<tds:Capabilities><tds:Network IPFilter="false" ZeroConfiguration="false" IPVersion6="false" '
                'DynDNS="false" NTP="0"/><tds:Security TLS1.0="false" TLS1.1="false" TLS1.2="false" '
                'OnboardKeyGeneration="false" AccessPolicyConfig="false" DefaultAccessPolicy="false" Dot1X="false" '
                'RemoteUserHandling="false" X.509Token="false" SAMLToken="false" KerberosToken="false" '
                'UsernameToken="true" HttpDigest="false" RELToken="false"/>'
                '<tds:System DiscoveryResolve="true" DiscoveryBye="true" RemoteDiscovery="false" SystemBackup="false" '
                'SystemLogging="false" FirmwareUpgrade="false"/></tds:Capabilities>')

    def _media_caps(self) -> str:
        return ('<trt:Capabilities SnapshotUri="true" Rotation="false" VideoSourceMode="false" OSD="false">'
                '<trt:ProfileCapabilities MaximumNumberOfProfiles="1"/>'
                '<trt:StreamingCapabilities RTPMulticast="false" RTP_TCP="true" RTP_RTSP_TCP="true" '
                'NonAggregateControl="false" NoRTSPStreaming="false"/></trt:Capabilities>')

    def system_date_and_time(self, r: Request) -> str:
        now = time.time()
        utc = datetime.fromtimestamp(now, timezone.utc)
        try:
            tz = ZoneInfo(self.identity.timezone)
        except (KeyError, ValueError):
            tz = timezone.utc
        local = datetime.fromtimestamp(now, tz)
        posix = _posix_tz(self.identity.timezone)
        return ("<tds:GetSystemDateAndTimeResponse><tds:SystemDateAndTime>"
                "<tt:DateTimeType>NTP</tt:DateTimeType>"
                f"<tt:DaylightSavings>{'true' if local.dst() else 'false'}</tt:DaylightSavings>"
                + (f"<tt:TimeZone><tt:TZ>{escape(posix)}</tt:TZ></tt:TimeZone>" if posix else "")
                + f"<tt:UTCDateTime>{_datetime(utc)}</tt:UTCDateTime><tt:LocalDateTime>{_datetime(local)}</tt:LocalDateTime>"
                "</tds:SystemDateAndTime></tds:GetSystemDateAndTimeResponse>")

    def set_system_date_and_time(self, r: Request) -> str:
        # The clock is chrony's; NVRs that only ask for NTP get a harmless yes
        if child_text(r.op, "tds:DateTimeType") == "NTP":
            return "<tds:SetSystemDateAndTimeResponse/>"
        raise invalid("ter:InvalidDateTime", "the camera's clock is set by NTP (chrony)")

    def scopes(self, r: Request) -> str:
        items = "".join(f"<tds:Scopes><tt:ScopeDef>Fixed</tt:ScopeDef><tt:ScopeItem>{escape(s)}</tt:ScopeItem></tds:Scopes>"
                        for s in self.identity.scopes())
        return f"<tds:GetScopesResponse>{items}</tds:GetScopesResponse>"

    def network_interfaces(self, r: Request) -> str:
        out = []
        for n in self.interfaces():
            cfg = (f"<tt:FromDHCP><tt:Address>{n.address}</tt:Address><tt:PrefixLength>{n.prefix}</tt:PrefixLength></tt:FromDHCP>"
                   if n.dhcp else
                   f"<tt:Manual><tt:Address>{n.address}</tt:Address><tt:PrefixLength>{n.prefix}</tt:PrefixLength></tt:Manual>")
            out.append(f'<tds:NetworkInterfaces token="{escape(n.name)}"><tt:Enabled>true</tt:Enabled>'
                       f"<tt:Info><tt:Name>{escape(n.name)}</tt:Name><tt:HwAddress>{escape(n.mac)}</tt:HwAddress>"
                       f"<tt:MTU>{n.mtu}</tt:MTU></tt:Info><tt:IPv4><tt:Enabled>true</tt:Enabled><tt:Config>"
                       f"{cfg}<tt:DHCP>{'true' if n.dhcp else 'false'}</tt:DHCP></tt:Config></tt:IPv4></tds:NetworkInterfaces>")
        return f"<tds:GetNetworkInterfacesResponse>{''.join(out)}</tds:GetNetworkInterfacesResponse>"

    def network_protocols(self, r: Request) -> str:
        return ("<tds:GetNetworkProtocolsResponse>"
                f"<tds:NetworkProtocols><tt:Name>HTTP</tt:Name><tt:Enabled>true</tt:Enabled><tt:Port>{r.port}</tt:Port></tds:NetworkProtocols>"
                f"<tds:NetworkProtocols><tt:Name>RTSP</tt:Name><tt:Enabled>true</tt:Enabled><tt:Port>{self.stream.rtsp_port}</tt:Port></tds:NetworkProtocols>"
                "</tds:GetNetworkProtocolsResponse>")

    def get_users(self, r: Request) -> str:
        users = "".join(f"<tds:User><tt:Username>{escape(u)}</tt:Username><tt:UserLevel>Administrator</tt:UserLevel></tds:User>"
                        for u in self.users)
        return f"<tds:GetUsersResponse>{users}</tds:GetUsersResponse>"

    # ---- Media -------------------------------------------------------------------

    def _check_profile(self, r: Request, path: str = "trt:ProfileToken") -> None:
        token = child_text(r.op, path)
        if token != PROFILE:
            raise invalid("ter:NoProfile", f"no profile {token!r}; the dome's profile is {PROFILE!r}")

    def _source_cfg(self, tag: str) -> str:
        n = self.stream.size
        return (f'<{tag} token="{SOURCE_CFG}"><tt:Name>Dome</tt:Name><tt:UseCount>1</tt:UseCount>'
                f'<tt:SourceToken>{SOURCE}</tt:SourceToken><tt:Bounds x="0" y="0" width="{n}" height="{n}"/></{tag}>')

    def _encoder_cfg(self, tag: str) -> str:
        s = self.stream
        return (f'<{tag} token="{ENCODER}"><tt:Name>H.264</tt:Name><tt:UseCount>1</tt:UseCount>'
                f"<tt:Encoding>H264</tt:Encoding><tt:Resolution><tt:Width>{s.size}</tt:Width><tt:Height>{s.size}</tt:Height></tt:Resolution>"
                "<tt:Quality>5</tt:Quality>"
                f"<tt:RateControl><tt:FrameRateLimit>{s.fps}</tt:FrameRateLimit><tt:EncodingInterval>1</tt:EncodingInterval>"
                f"<tt:BitrateLimit>{s.bitrate_kbps}</tt:BitrateLimit></tt:RateControl>"
                f"<tt:H264><tt:GovLength>{s.gov}</tt:GovLength><tt:H264Profile>Baseline</tt:H264Profile></tt:H264>"
                "<tt:Multicast><tt:Address><tt:Type>IPv4</tt:Type><tt:IPv4Address>0.0.0.0</tt:IPv4Address></tt:Address>"
                "<tt:Port>0</tt:Port><tt:TTL>0</tt:TTL><tt:AutoStart>false</tt:AutoStart></tt:Multicast>"
                f"<tt:SessionTimeout>PT60S</tt:SessionTimeout></{tag}>")

    def _profile(self, tag: str) -> str:
        return (f'<{tag} token="{PROFILE}" fixed="true"><tt:Name>Dome</tt:Name>'
                f"{self._source_cfg('tt:VideoSourceConfiguration')}{self._encoder_cfg('tt:VideoEncoderConfiguration')}</{tag}>")

    def get_profile(self, r: Request) -> str:
        self._check_profile(r)
        return f"<trt:GetProfileResponse>{self._profile('trt:Profile')}</trt:GetProfileResponse>"

    def video_sources(self, r: Request) -> str:
        s = self.stream
        return (f'<trt:GetVideoSourcesResponse><trt:VideoSources token="{SOURCE}"><tt:Framerate>{s.fps}</tt:Framerate>'
                f"<tt:Resolution><tt:Width>{s.size}</tt:Width><tt:Height>{s.size}</tt:Height></tt:Resolution>"
                "</trt:VideoSources></trt:GetVideoSourcesResponse>")

    def video_source_configuration(self, r: Request) -> str:
        if child_text(r.op, "trt:ConfigurationToken") != SOURCE_CFG:
            raise invalid("ter:NoConfig", "no such video source configuration")
        return f"<trt:GetVideoSourceConfigurationResponse>{self._source_cfg('trt:Configuration')}</trt:GetVideoSourceConfigurationResponse>"

    def compatible_source_cfgs(self, r: Request) -> str:
        self._check_profile(r)
        return f"<trt:GetCompatibleVideoSourceConfigurationsResponse>{self._source_cfg('trt:Configurations')}</trt:GetCompatibleVideoSourceConfigurationsResponse>"

    def video_source_configuration_options(self, r: Request) -> str:
        n = self.stream.size
        return ("<trt:GetVideoSourceConfigurationOptionsResponse><trt:Options>"
                f"<tt:BoundsRange><tt:XRange><tt:Min>0</tt:Min><tt:Max>0</tt:Max></tt:XRange><tt:YRange><tt:Min>0</tt:Min><tt:Max>0</tt:Max></tt:YRange>"
                f"<tt:WidthRange><tt:Min>{n}</tt:Min><tt:Max>{n}</tt:Max></tt:WidthRange><tt:HeightRange><tt:Min>{n}</tt:Min><tt:Max>{n}</tt:Max></tt:HeightRange></tt:BoundsRange>"
                f"<tt:VideoSourceTokensAvailable>{SOURCE}</tt:VideoSourceTokensAvailable>"
                "</trt:Options></trt:GetVideoSourceConfigurationOptionsResponse>")

    def video_encoder_configuration(self, r: Request) -> str:
        if child_text(r.op, "trt:ConfigurationToken") != ENCODER:
            raise invalid("ter:NoConfig", "no such video encoder configuration")
        return f"<trt:GetVideoEncoderConfigurationResponse>{self._encoder_cfg('trt:Configuration')}</trt:GetVideoEncoderConfigurationResponse>"

    def compatible_encoder_cfgs(self, r: Request) -> str:
        self._check_profile(r)
        return f"<trt:GetCompatibleVideoEncoderConfigurationsResponse>{self._encoder_cfg('trt:Configurations')}</trt:GetCompatibleVideoEncoderConfigurationsResponse>"

    def video_encoder_configuration_options(self, r: Request) -> str:
        s = self.stream
        return ("<trt:GetVideoEncoderConfigurationOptionsResponse><trt:Options>"
                "<tt:QualityRange><tt:Min>5</tt:Min><tt:Max>5</tt:Max></tt:QualityRange><tt:H264>"
                f"<tt:ResolutionsAvailable><tt:Width>{s.size}</tt:Width><tt:Height>{s.size}</tt:Height></tt:ResolutionsAvailable>"
                f"<tt:GovLengthRange><tt:Min>{s.gov}</tt:Min><tt:Max>{s.gov}</tt:Max></tt:GovLengthRange>"
                f"<tt:FrameRateRange><tt:Min>{s.fps}</tt:Min><tt:Max>{s.fps}</tt:Max></tt:FrameRateRange>"
                "<tt:EncodingIntervalRange><tt:Min>1</tt:Min><tt:Max>1</tt:Max></tt:EncodingIntervalRange>"
                "<tt:H264ProfilesSupported>Baseline</tt:H264ProfilesSupported></tt:H264>"
                "</trt:Options></trt:GetVideoEncoderConfigurationOptionsResponse>")

    def set_video_encoder_configuration(self, r: Request) -> str:
        """The stream's shape never changes (D-015), so only a matching
        resolution, codec and frame rate is accepted; bitrate, quality and GOP
        an NVR pushes along with them are left as they are."""
        cfg = r.op.find("trt:Configuration", NS)
        s = self.stream
        if cfg is None or cfg.get("token") != ENCODER:
            raise invalid("ter:NoConfig", "no such video encoder configuration")
        want = {"tt:Encoding": "H264", "tt:Resolution/tt:Width": str(s.size), "tt:Resolution/tt:Height": str(s.size)}
        for path, value in want.items():
            got = child_text(cfg, path)
            if got is not None and got != value:
                raise invalid("ter:ConfigModify", f"the dome stream is fixed at {s.size}x{s.size} H.264 (D-015)")
        fps = child_text(cfg, "tt:RateControl/tt:FrameRateLimit")
        if fps is not None and float(fps) != s.fps:
            raise invalid("ter:ConfigModify", f"the dome stream is fixed at {s.fps} fps (D-015)")
        return "<trt:SetVideoEncoderConfigurationResponse/>"

    def stream_uri(self, r: Request) -> str:
        self._check_profile(r)
        return _media_uri("GetStreamUri", f"rtsp://{r.host}:{self.stream.rtsp_port}/{self.stream.path}")

    def snapshot_uri(self, r: Request) -> str:
        self._check_profile(r)
        return _media_uri("GetSnapshotUri", f"{r.base}/onvif/snapshot.jpg")

    # ---- Imaging -----------------------------------------------------------------

    def _with_source(self, handler):
        def checked(r: Request) -> str:
            token = child_text(r.op, "timg:VideoSourceToken")
            if token != SOURCE:
                raise invalid("ter:NoSource", f"no video source {token!r}; the dome's is {SOURCE!r}")
            return handler(r)
        return checked

    def imaging_settings(self, r: Request) -> str:
        return self._with_source(lambda r: (
            f"<timg:GetImagingSettingsResponse><timg:ImagingSettings>{self._settings_xml()}"
            "</timg:ImagingSettings></timg:GetImagingSettingsResponse>"))(r)

    def _settings_xml(self) -> str:
        ctrls, v = self.settings.controls(), self.settings.effective()
        out = []
        if "backlight_compensation" in ctrls:
            level = v["backlight_compensation"]
            out.append(f"<tt:BacklightCompensation><tt:Mode>{'ON' if level else 'OFF'}</tt:Mode>"
                       f"<tt:Level>{level}</tt:Level></tt:BacklightCompensation>")
        for onvif, v4l in _LEVELS:
            if v4l in ctrls:
                out.append(f"<tt:{onvif}>{v[v4l]}</tt:{onvif}>")
        exp, per, _, manual = _exposure_map(self.settings)
        if "auto_exposure" in ctrls:
            mode = "MANUAL" if v["auto_exposure"] == manual else "AUTO"
            out.append(f"<tt:Exposure><tt:Mode>{mode}</tt:Mode>"
                       + (f"<tt:ExposureTime>{v[exp] * per}</tt:ExposureTime>" if exp in ctrls else "")
                       + (f"<tt:Gain>{v['gain']}</tt:Gain>" if "gain" in ctrls else "")
                       + "</tt:Exposure>")
        if "sharpness" in ctrls:
            out.append(f"<tt:Sharpness>{v['sharpness']}</tt:Sharpness>")
        if "white_balance_automatic" in ctrls:
            out.append(f"<tt:WhiteBalance><tt:Mode>{'AUTO' if v['white_balance_automatic'] else 'MANUAL'}</tt:Mode></tt:WhiteBalance>")
        return "".join(out)

    def imaging_options(self, r: Request) -> str:
        def rng(lo, hi):
            return f"<tt:Min>{lo}</tt:Min><tt:Max>{hi}</tt:Max>"

        def body(_r):
            c = self.settings.controls()
            out = []
            if "backlight_compensation" in c:
                b = c["backlight_compensation"]
                out.append("<tt:BacklightCompensation><tt:Mode>OFF</tt:Mode><tt:Mode>ON</tt:Mode>"
                           f"<tt:Level>{rng(b.minimum, b.maximum)}</tt:Level></tt:BacklightCompensation>")
            for onvif, v4l in _LEVELS:
                if v4l in c:
                    out.append(f"<tt:{onvif}>{rng(c[v4l].minimum, c[v4l].maximum)}</tt:{onvif}>")
            exp, per, _, _ = _exposure_map(self.settings)
            if "auto_exposure" in c:
                out.append("<tt:Exposure><tt:Mode>AUTO</tt:Mode><tt:Mode>MANUAL</tt:Mode>"
                           + (f"<tt:ExposureTime>{rng(c[exp].minimum * per, c[exp].maximum * per)}</tt:ExposureTime>"
                              if exp in c else "")
                           + (f"<tt:Gain>{rng(c['gain'].minimum, c['gain'].maximum)}</tt:Gain>" if "gain" in c else "")
                           + "</tt:Exposure>")
            if "sharpness" in c:
                out.append(f"<tt:Sharpness>{rng(c['sharpness'].minimum, c['sharpness'].maximum)}</tt:Sharpness>")
            if "white_balance_automatic" in c:
                out.append("<tt:WhiteBalance><tt:Mode>AUTO</tt:Mode><tt:Mode>MANUAL</tt:Mode></tt:WhiteBalance>")
            return f"<timg:GetOptionsResponse><timg:ImagingOptions>{''.join(out)}</timg:ImagingOptions></timg:GetOptionsResponse>"
        return self._with_source(body)(r)

    def set_imaging_settings(self, r: Request) -> str:
        """Settings the cameras support are applied to all of them and kept;
        fields ONVIF has but these cameras don't (focus, IR cut filter, wide
        dynamic range, white balance gains) are ignored."""
        def body(r):
            s = r.op.find("timg:ImagingSettings", NS)
            if s is None:
                raise invalid("ter:SettingsInvalid", "no ImagingSettings")
            changes = imaging_changes(s, self.settings.effective(), _exposure_map(self.settings))
            if changes:
                try:
                    self.settings.update(changes)
                except ValueError as e:
                    raise invalid("ter:SettingsInvalid", str(e)) from None
                self.log(f"onvif: {r.user or '?'} set {changes}")
            return "<timg:SetImagingSettingsResponse/>"
        return self._with_source(body)(r)


def imaging_changes(s: ET.Element, current: dict[str, int],
                    exposure: tuple[str, int, int, int] = CameraSettings.ONVIF_EXPOSURE) -> dict[str, int]:
    """ONVIF ImagingSettings20 -> control values in the cameras' units
    (exposure: see _exposure_map)."""
    exp, per, auto, manual = exposure
    changes: dict[str, int] = {}

    def num(path: str) -> int | None:
        text = child_text(s, path)
        if text is None:
            return None
        try:
            return round(float(text))
        except ValueError:
            raise invalid("ter:SettingsInvalid", f"{path} {text!r} is not a number") from None

    bl_mode = child_text(s, "tt:BacklightCompensation/tt:Mode")
    bl_level = num("tt:BacklightCompensation/tt:Level")
    if bl_mode == "OFF":
        changes["backlight_compensation"] = 0
    elif bl_mode == "ON" or bl_level is not None:
        level = bl_level if bl_level is not None else current.get("backlight_compensation", 0)
        if bl_mode == "ON" and not level:
            level = 48   # the firmware default; ON with no level
        changes["backlight_compensation"] = level
    for onvif, v4l in _LEVELS + (("Sharpness", "sharpness"),):
        value = num(f"tt:{onvif}")
        if value is not None:
            changes[v4l] = value

    mode = child_text(s, "tt:Exposure/tt:Mode")
    if mode is not None:
        if mode not in ("AUTO", "MANUAL"):
            raise invalid("ter:SettingsInvalid", f"exposure mode {mode!r}")
        changes["auto_exposure"] = manual if mode == "MANUAL" else auto
    t = child_text(s, "tt:Exposure/tt:ExposureTime")
    if t is not None:
        try:
            changes[exp] = max(1, round(float(t) / per))
        except ValueError:
            raise invalid("ter:SettingsInvalid", f"exposure time {t!r} is not a number") from None
    gain = num("tt:Exposure/tt:Gain")
    if gain is not None:
        changes["gain"] = gain

    wb = child_text(s, "tt:WhiteBalance/tt:Mode")
    if wb is not None:
        if wb not in ("AUTO", "MANUAL"):
            raise invalid("ter:SettingsInvalid", f"white balance mode {wb!r}")
        changes["white_balance_automatic"] = 1 if wb == "AUTO" else 0

    # Only what actually changes, so an NVR echoing the settings back is a no-op
    return {k: v for k, v in changes.items() if current.get(k) != v}


def _media_uri(op: str, uri: str) -> str:
    return (f"<trt:{op}Response><trt:MediaUri><tt:Uri>{escape(uri)}</tt:Uri>"
            "<tt:InvalidAfterConnect>false</tt:InvalidAfterConnect><tt:InvalidAfterReboot>false</tt:InvalidAfterReboot>"
            f"<tt:Timeout>PT0S</tt:Timeout></trt:MediaUri></trt:{op}Response>")


def _datetime(t: datetime) -> str:
    return (f"<tt:Time><tt:Hour>{t.hour}</tt:Hour><tt:Minute>{t.minute}</tt:Minute><tt:Second>{t.second}</tt:Second></tt:Time>"
            f"<tt:Date><tt:Year>{t.year}</tt:Year><tt:Month>{t.month}</tt:Month><tt:Day>{t.day}</tt:Day></tt:Date>")


def _posix_tz(name: str, zoneinfo_dir: Path = Path("/usr/share/zoneinfo")) -> str | None:
    """The POSIX TZ string ONVIF wants ('MST7'), from the TZif file's footer."""
    try:
        data = (zoneinfo_dir / name).read_bytes()
    except (OSError, ValueError):
        return None
    if not data.startswith(b"TZif") or not data.endswith(b"\n"):
        return None
    footer = data[data.rstrip(b"\n").rfind(b"\n") + 1:].strip()
    return footer.decode(errors="ignore") or None


def _raise(f: Fault):
    raise f
