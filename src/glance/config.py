"""Store configuration: floor plan, cameras + calibration, zones, thresholds, privacy.

One YAML file describes one store. All geometry that matters for analytics lives in *floor
coordinates* (metres, origin top-left of the floor plan, x right, y down) so that heatmaps, zones
and multi-camera setups share one frame of reference. Cameras only need a calibration: four or
more pixel<->floor point pairs.
"""
from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

Point = tuple[float, float]


class FloorPlan(BaseModel):
    width_m: float = Field(gt=0)
    height_m: float = Field(gt=0)


class CameraPose(BaseModel):
    """Where the camera hangs, for the floor-plan drawing and sanity checks. The calibration points, not this,
    define the geometry the analytics use."""
    x: float
    y: float
    height_m: float = 3.0
    heading_deg: float = 0.0  # direction the camera looks, degrees clockwise from +x (floor coordinates, y down)
    fov_deg: float = 90.0


class CameraConfig(BaseModel):
    id: str
    name: str | None = None
    # entrance: counts door crossings and defines "visitors" (one per person entering) when there are several cameras
    # floor:    measures its zones (impressions, dwell, engagement) but does not define visitor totals
    # both:     a single camera that does everything (the default, and the only sensible choice with one camera)
    role: Literal["entrance", "floor", "both"] = "both"
    # "center" for ceiling/overhead cameras (the blob centre is the person's floor position).
    # "bottom_center" for oblique cameras (the feet are the floor contact point).
    anchor: Literal["center", "bottom_center"] = "center"
    pixel_points: list[Point]
    floor_points: list[Point]
    # Pixel regions to ignore (mirrors, TV screens, posters of people): detections anchored there are dropped.
    mask_polygons: list[list[Point]] = Field(default_factory=list)
    # Pixel regions blanked BEFORE detection (restrooms, fitting-room doors, staff-only rooms): the detector never
    # sees them, so nothing there can be tracked, counted or analysed.
    privacy_mask_polygons: list[list[Point]] = Field(default_factory=list)
    image_size: tuple[int, int] | None = None  # (width, height) in pixels of the analysed stream
    pose: CameraPose | None = None
    # Floor polygon (metres) where this camera's measurements are trusted; None = everything it can see.
    valid_region: list[Point] | None = None
    # Zone ids this camera is responsible for. None = chosen automatically (best view of the zone).
    owns: list[str] | None = None
    # Per-camera detector tuning, merged over ``detector.params``. Pixel sizes differ between cameras, so e.g. the
    # background-subtraction detector's ``typical_area`` must too: ``glance cameras`` prints a suggestion.
    detector_params: dict = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check_points(self) -> "CameraConfig":
        if len(self.pixel_points) < 4 or len(self.pixel_points) != len(self.floor_points):
            raise ValueError("camera needs >=4 pixel_points with matching floor_points")
        return self


class ZoneConfig(BaseModel):
    id: str
    name: str
    # fixture: a display/shelf with an exposure zone and a target to face (impressions + engagement)
    # area:    traffic/dwell only (checkout queue, aisle, entrance lobby), no facing test
    # staff:   back-of-house; people who dwell here are classified as staff
    kind: Literal["fixture", "area", "staff"] = "fixture"
    polygon: list[Point]
    target: Point | None = None
    min_engage_s: float | None = None

    @field_validator("polygon")
    @classmethod
    def _poly(cls, v: list[Point]) -> list[Point]:
        if len(v) < 3:
            raise ValueError("zone polygon needs >=3 points")
        return v


class EntranceConfig(BaseModel):
    p1: Point
    p2: Point
    inside: Point  # any floor point on the *inside* of the store; defines the entry direction
    hysteresis_m: float = 0.25
    camera: str | None = None  # id of the camera that sees the door (None = the first camera)


class StaffConfig(BaseModel):
    # OpenCV HSV ranges (H 0-179). A list so red (which wraps) can use two ranges.
    hsv_ranges: list[tuple[tuple[int, int, int], tuple[int, int, int]]] = Field(default_factory=list)
    min_fraction: float = 0.18  # fraction of the central box pixels in range to vote "staff"
    vote_ratio: float = 0.5  # fraction of frames voting staff to label the track staff
    zone_time_fraction: float = 0.7  # fraction of time in a staff zone to label staff


class Thresholds(BaseModel):
    process_fps: float = 10.0
    smooth_window_s: float = 1.0  # displacement window used for speed / heading
    v_slow_mps: float = 0.35  # below this a person counts as "stopped"
    v_move_mps: float = 0.45  # above this heading is considered reliable (motion facing)
    facing_cone_deg: float = 60.0  # half-angle of the cone around the bearing to the display
    min_engage_s: float = 2.0  # stopped (AND facing, in stop_facing mode) this long => engaged impression
    # "stop":        engaged = stopped inside the exposure zone >= min_engage_s (the MVP default)
    # "stop_facing": additionally require facing the display. Needs a real orientation estimator
    #                (pose/head); with motion-only heading it LOSES recall, see `glance validate`.
    engagement_mode: Literal["stop", "stop_facing"] = "stop"
    min_zone_visit_s: float = 0.5
    zone_gap_merge_s: float = 3.0
    min_track_s: float = 1.5
    static_max_disp_m: float = 0.3  # tracks that barely move for a long time are clutter (mannequin)
    static_min_duration_s: float = 90.0
    lost_buffer_s: float = 2.0  # tracker coasting time before a track is considered ended
    stitch_window_s: float = 3.0  # re-link a new track to one that ended this recently ...
    stitch_max_dist_m: float = 1.3  # ... and this close (motion only, no appearance)
    stitch_edge_margin_m: float = 0.9  # never stitch across the floor edge: that is people entering/leaving
    track_activation: float = 0.35
    high_conf: float = 0.5
    min_consecutive: int = 3


class DetectorConfig(BaseModel):
    kind: Literal["bgsub", "yolox_onnx", "rfdetr"] = "bgsub"
    params: dict = Field(default_factory=dict)


class PrivacyConfig(BaseModel):
    # "head": blur the head region only (overhead: central box; oblique: top of the box)
    # "person": blur the entire person box (strongest; recommended for overhead/low-res cameras)
    # "none": explicit opt-out; the preview writer then refuses to run without --allow-raw
    blur: Literal["head", "person", "none"] = "person"
    head_fraction: float = 0.35
    margin: float = 0.25  # expand the blur region by this fraction (fail safe)
    persist_trajectories: bool = False  # MVP never writes raw trajectories to the database
    id_rotation_hours: int = 24


class OutputConfig(BaseModel):
    bucket_minutes: int = 5
    heat_cell_m: float = 0.5
    timezone: str = "America/Los_Angeles"


class DemographicsConfig(BaseModel):
    """Optional audience estimates (age band, perceived gender). OFF by default.

    When on, a face-region model runs on the edge box, IN MEMORY, once per tracked person; the result is added to an
    aggregate counter and the person's state is dropped. Nothing per-person is stored: the ``audience`` table has
    no visit key, only counts per (time bucket, display, age band, perceived gender). See docs/privacy-and-legal.md.
    """
    enabled: bool = False
    age_bands: list[str] = Field(default_factory=lambda: ["<13", "13-24", "25-44", "45-64", "65+"])
    min_cell: int = Field(default=10, ge=2)  # groups smaller than this are never shown (merged or hidden)
    bucket_hours: int = Field(default=6, ge=1, le=24)  # time resolution of the stored counts (coarse on purpose)


class BusinessConfig(BaseModel):
    """Assumptions behind the what-if estimates in Recommendations. They are INPUTS, not measurements: replace them
    with the store's own numbers (POS data) as soon as they are available."""
    currency: str = "USD"
    open_hours_per_day: float = Field(default=10.0, gt=0)
    avg_item_value: float = Field(default=28.0, ge=0)  # average value of what a shopper buys after stopping at a display
    # low / high share of engaged shoppers who buy something because of the display (illustrative defaults)
    purchase_rate_given_engaged: tuple[float, float] = (0.05, 0.15)
    visitors_per_day: float | None = None  # None = measured visitors per hour x open_hours_per_day

    @model_validator(mode="after")
    def _rates(self) -> "BusinessConfig":
        lo, hi = self.purchase_rate_given_engaged
        if not (0 <= lo <= hi <= 1):
            raise ValueError("purchase_rate_given_engaged must be (low, high) with 0 <= low <= high <= 1")
        return self


class StoreConfig(BaseModel):
    name: str = "Demo Store"
    # The floor plan changes when the store is rearranged. Bump this then: metrics are tagged with it, so a before/after
    # comparison across two layout versions is flagged instead of silently mixing layouts.
    layout_version: str = "1"
    floor: FloorPlan
    cameras: list[CameraConfig]
    zones: list[ZoneConfig]
    entrance: EntranceConfig
    staff: StaffConfig = Field(default_factory=StaffConfig)
    thresholds: Thresholds = Field(default_factory=Thresholds)
    detector: DetectorConfig = Field(default_factory=DetectorConfig)
    privacy: PrivacyConfig = Field(default_factory=PrivacyConfig)
    output: OutputConfig = Field(default_factory=OutputConfig)
    demographics: DemographicsConfig = Field(default_factory=DemographicsConfig)
    business: BusinessConfig = Field(default_factory=BusinessConfig)

    @model_validator(mode="after")
    def _unique_ids(self) -> "StoreConfig":
        ids = [z.id for z in self.zones]
        if len(ids) != len(set(ids)):
            raise ValueError("zone ids must be unique")
        cams = [c.id for c in self.cameras]
        if len(cams) != len(set(cams)):
            raise ValueError("camera ids must be unique")
        if not cams:
            raise ValueError("at least one camera is required")
        known, owned = set(ids), {}
        for c in self.cameras:
            for zid in c.owns or []:
                if zid not in known:
                    raise ValueError(f"camera {c.id} owns unknown zone {zid!r}")
                if zid in owned:
                    raise ValueError(f"zone {zid!r} is owned by both {owned[zid]} and {c.id}: one owner per zone")
                owned[zid] = c.id
        if self.entrance.camera is not None and self.entrance.camera not in cams:
            raise ValueError(f"entrance.camera {self.entrance.camera!r} is not a configured camera")
        return self

    @property
    def multi_camera(self) -> bool:
        return len(self.cameras) > 1

    def zone(self, zone_id: str) -> ZoneConfig:
        for z in self.zones:
            if z.id == zone_id:
                return z
        raise KeyError(zone_id)

    def camera(self, camera_id: str | None = None) -> CameraConfig:
        if camera_id is None:
            return self.cameras[0]
        for c in self.cameras:
            if c.id == camera_id:
                return c
        raise KeyError(camera_id)

    def measured_zones(self) -> list[ZoneConfig]:
        """Zones that produce customer metrics (everything except back-of-house)."""
        return [z for z in self.zones if z.kind != "staff"]

    def engage_seconds(self, zone: ZoneConfig) -> float:
        return zone.min_engage_s if zone.min_engage_s is not None else self.thresholds.min_engage_s


def load_config(path: str | Path) -> StoreConfig:
    with open(path, "r", encoding="utf-8") as fh:
        return StoreConfig.model_validate(yaml.safe_load(fh))


def dump_config(cfg: StoreConfig, path: str | Path) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        yaml.safe_dump(cfg.model_dump(mode="json"), fh, sort_keys=False)
