#!/usr/bin/env python3
"""
Parking Mode Monitor - Intelligent dashcam for parked vehicles

Uses statistical analysis (Mahalanobis distance) to detect impacts
and preserve footage around detected shocks.
"""

import json
import math
import time
from datetime import datetime
from pathlib import Path

import numpy as np

import cereal.messaging as messaging
from cereal import custom
from openpilot.common.params import Params
from openpilot.common.realtime import Ratekeeper
from openpilot.common.swaglog import cloudlog
from openpilot.system.hardware.hw import Paths
from openpilot.system.loggerd.xattr_cache import setxattr


class ParkingMonitor:
  def __init__(self):
    self.params = Params()
    self.sm = messaging.SubMaster([
      'accelerometer',
      'deviceState',
      'pandaStates',
      'carState'
    ])
    self.pm = messaging.PubMaster(['parkingEvent'])

    # State
    self.parking_mode_active = False
    self.is_calibrating = False
    self.calibration_data: list = []
    self.mean_accel: np.ndarray | None = None
    self.cov_inv_accel: np.ndarray | None = None

    # Config
    self.MAHALANOBIS_THRESHOLD = 15.0  # Statistical distance threshold for shock detection
    self.CALIBRATION_SAMPLES = 100     # 10 seconds at 10Hz to calibrate baseline vibrations
    self.PRESERVE_BEFORE = 2           # Preserve 2 segments before shock (2 x 60s = 2min)
    self.PRESERVE_AFTER = 4            # Preserve 4 segments after shock (4 x 60s = 4min)
    self.LOW_VOLTAGE_THRESHOLD = 11.8  # Shutdown if battery < 11.8V
    self.MAX_PARKING_DURATION_SEC = 12 * 3600  # 12 hours max parking duration

    self.parking_start_time = 0
    self.rk = Ratekeeper(10)

  def is_parked(self) -> bool:
    if not self.sm.valid['pandaStates']:
      return False

    # Check ignition is off
    ignition_off = all(not ps.ignitionLine for ps in self.sm['pandaStates'])

    # Check vehicle is not moving (speed < 0.5 m/s ~= 1.8 km/h)
    standstill = True
    if self.sm.valid['carState']:
      standstill = self.sm['carState'].vEgo < 0.5

    return ignition_off and standstill

  def calibrate(self):
    if len(self.calibration_data) < self.CALIBRATION_SAMPLES:
      return

    data = np.array(self.calibration_data)
    self.mean_accel = np.mean(data, axis=0)

    try:
      cov = np.cov(data, rowvar=False)
      self.cov_inv_accel = np.linalg.inv(cov)
      cloudlog.info(f"Parking mode calibrated. Mean: {self.mean_accel}, Covariance: {cov}")
    except np.linalg.LinAlgError:
      # Covariance matrix is singular, use a default identity matrix
      cloudlog.warning("Singular covariance matrix, using identity.")
      self.cov_inv_accel = np.identity(3)

    self.is_calibrating = False
    self.calibration_data = []  # Clear data after calibration

  def detect_shock_mahalanobis(self) -> tuple[bool, float]:
    if self.mean_accel is None or self.cov_inv_accel is None or not self.sm.valid['accelerometer']:
      return False, 0.0

    current_accel = np.array(self.sm['accelerometer'].acceleration.v)
    diff = current_accel - self.mean_accel

    # Mahalanobis distance squared: D^2 = (x - mu)^T * Sigma^-1 * (x - mu)
    mahal_dist_sq = diff.T @ self.cov_inv_accel @ diff

    is_shock = mahal_dist_sq > self.MAHALANOBIS_THRESHOLD
    return is_shock, math.sqrt(mahal_dist_sq)

  def check_battery_voltage(self) -> float:
    if not self.sm.valid['deviceState']:
      return 13.0  # Default safe value

    return self.sm['deviceState'].batteryVoltage

  def should_shutdown(self) -> bool:
    voltage = self.check_battery_voltage()
    if voltage < self.LOW_VOLTAGE_THRESHOLD:
      cloudlog.warning(f"Low voltage detected: {voltage:.2f}V")
      return True

    # Check max duration
    if self.parking_start_time > 0:
      elapsed = time.monotonic() - self.parking_start_time
      if elapsed > self.MAX_PARKING_DURATION_SEC:
        cloudlog.info("Max parking duration reached")
        return True

    return False

  def preserve_segments(self, shock_time: float):
    """Mark segments around shock time for preservation (no deletion)"""
    realdata_path = Path(Paths.log_root()) / "realdata"
    if not realdata_path.exists():
      return

    segments = sorted(realdata_path.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True)

    # Calculate time window
    preserve_start = shock_time - (self.PRESERVE_BEFORE * 60)
    preserve_end = shock_time + (self.PRESERVE_AFTER * 60)

    for segment in segments:
      seg_time = segment.stat().st_mtime
      if preserve_start <= seg_time <= preserve_end:
        try:
          # Mark as preserved (will prevent deletion by deleter.py)
          setxattr(str(segment), "user.preserve", b"1")
          cloudlog.info(f"Preserved segment: {segment.name}")
        except Exception as e:
          cloudlog.error(f"Failed to preserve segment {segment.name}: {e}")

  def create_shock_alert(self, shock_time: float):
    """Create an offroad alert for the detected shock"""
    timestamp_str = datetime.fromtimestamp(shock_time).strftime("%Y-%m-%d %H:%M:%S")
    alert_data = {
      "text": f"Parking Mode: Shock detected at {timestamp_str}. Footage has been preserved.",
      "extra": timestamp_str
    }
    self.params.put("Offroad_ParkingShock", json.dumps(alert_data))

  def publish_state(self, state: str, shock_detected: bool = False, shock_intensity: float = 0.0):
    msg = messaging.new_message('parkingEvent')
    evt = msg.parkingEvent

    state_map = {
      'disabled': custom.ParkingEvent.ParkingState.disabled,
      'calibrating': custom.ParkingEvent.ParkingState.calibrating,
      'monitoring': custom.ParkingEvent.ParkingState.monitoring,
      'shockDetected': custom.ParkingEvent.ParkingState.shockDetected,
      'lowBattery': custom.ParkingEvent.ParkingState.lowBattery,
      'shutdownPending': custom.ParkingEvent.ParkingState.shutdownPending,
    }

    evt.state = state_map.get(state, custom.ParkingEvent.ParkingState.disabled)
    evt.shockDetected = shock_detected
    evt.shockIntensity = shock_intensity
    evt.batteryVoltage = self.check_battery_voltage()
    evt.calibrationProgress = len(self.calibration_data) / self.CALIBRATION_SAMPLES if self.is_calibrating else 1.0
    evt.timestamp = int(time.time() * 1000)

    self.pm.send('parkingEvent', msg)

  def run(self):
    cloudlog.info("Parking monitor started")

    while True:
      self.sm.update(1000)

      # Check if parking mode is enabled
      parking_enabled = self.params.get_bool("ParkingModeEnabled")

      if not parking_enabled:
        if self.parking_mode_active:
          cloudlog.info("Parking mode disabled")
          self.parking_mode_active = False
          self.params.put_bool("ParkingModeActive", False)
        self.publish_state('disabled')
        self.rk.keep_time()
        continue

      # Check if vehicle is parked
      if not self.is_parked():
        if self.parking_mode_active:
          cloudlog.info("Vehicle no longer parked, deactivating parking mode")
          self.parking_mode_active = False
          self.is_calibrating = False
          self.calibration_data = []
          self.mean_accel = None
          self.cov_inv_accel = None
          self.params.put_bool("ParkingModeActive", False)
        self.publish_state('disabled')
        self.rk.keep_time()
        continue

      # Activate parking mode if not already active
      if not self.parking_mode_active:
        cloudlog.info("Vehicle parked, activating parking mode")
        self.parking_mode_active = True
        self.is_calibrating = True
        self.calibration_data = []
        self.parking_start_time = time.monotonic()
        self.params.put_bool("ParkingModeActive", True)

      # Check for low battery / shutdown conditions
      if self.should_shutdown():
        self.publish_state('shutdownPending')
        cloudlog.warning("Parking mode shutdown due to low battery or timeout")
        self.params.put_bool("DoShutdown", True)
        break

      # Calibration phase
      if self.is_calibrating:
        if self.sm.valid['accelerometer']:
          accel = list(self.sm['accelerometer'].acceleration.v)
          self.calibration_data.append(accel)
          self.calibrate()
        self.publish_state('calibrating')
        self.rk.keep_time()
        continue

      # Monitoring phase - detect shocks
      is_shock, intensity = self.detect_shock_mahalanobis()

      if is_shock:
        shock_time = time.time()
        cloudlog.warning(f"Shock detected! Intensity: {intensity:.2f}")
        self.params.put("ParkingModeLastShock", str(int(shock_time)))
        self.preserve_segments(shock_time)
        self.create_shock_alert(shock_time)
        self.publish_state('shockDetected', shock_detected=True, shock_intensity=intensity)
      else:
        self.publish_state('monitoring')

      self.rk.keep_time()


def main():
  pm = ParkingMonitor()
  pm.run()


if __name__ == "__main__":
  main()
