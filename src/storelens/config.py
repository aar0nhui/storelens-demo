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


class CameraConfig(BaseModel):
    id: str
    # "center" for ceiling/overhead cameras (the blob centre is the person's floor position).
    # "bottom_center" for oblique cameras (the feet are the floor contact point).
    anchor: Literal["center", "bottom_center"] = "center"
    pixel_points: list[Point]
    floor_points: list[Point]
    # Pixel regions to ignore (mirrors, TV screens, posters of people).
    mask_polygons: list[list[Point]] = Field(default_factory=list)

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
    #                (pose/head); with motion-only heading it LOSES recall, see `storelens validate`.
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


class StoreConfig(BaseModel):
    name: str = "Demo Store"
    floor: FloorPlan
    cameras: list[CameraConfig]
    zones: list[ZoneConfig]
    entrance: EntranceConfig
    staff: StaffConfig = Field(default_factory=StaffConfig)
    thresholds: Thresholds = Field(default_factory=Thresholds)
    detector: DetectorConfig = Field(default_factory=DetectorConfig)
    privacy: PrivacyConfig = Field(default_factory=PrivacyConfig)
    output: OutputConfig = Field(default_factory=OutputConfig)

    @model_validator(mode="after")
    def _unique_ids(self) -> "StoreConfig":
        ids = [z.id for z in self.zones]
        if len(ids) != len(set(ids)):
            raise ValueError("zone ids must be unique")
        return self

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
