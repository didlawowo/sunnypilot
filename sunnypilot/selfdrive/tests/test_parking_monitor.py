#!/usr/bin/env python3
"""
Test script for Parking Monitor

Simulates accelerometer data and tests shock detection.
Run this on a comma device or in simulation environment.
"""

import time
import threading
import numpy as np

import cereal.messaging as messaging
from openpilot.common.params import Params


class ParkingMonitorTester:
  def __init__(self):
    self.params = Params()
    self.pm = messaging.PubMaster(['accelerometer', 'deviceState', 'pandaStates', 'carState'])
    self.sm = messaging.SubMaster(['parkingEvent'])
    self.running = False

  def send_accelerometer(self, x: float, y: float, z: float):
    """Send accelerometer reading"""
    msg = messaging.new_message('accelerometer')
    msg.accelerometer.acceleration.v = [x, y, z]
    msg.accelerometer.timestamp = int(time.time() * 1e9)
    self.pm.send('accelerometer', msg)

  def send_device_state(self, voltage: float = 12.5):
    """Send device state with battery voltage"""
    msg = messaging.new_message('deviceState')
    msg.deviceState.batteryVoltage = voltage
    self.pm.send('deviceState', msg)

  def send_panda_states(self, ignition: bool = False):
    """Send panda states (ignition off = parked)"""
    msg = messaging.new_message('pandaStates', 1)
    msg.pandaStates[0].ignitionLine = ignition
    self.pm.send('pandaStates', msg)

  def send_car_state(self, speed: float = 0.0):
    """Send car state (speed)"""
    msg = messaging.new_message('carState')
    msg.carState.vEgo = speed
    self.pm.send('carState', msg)

  def simulate_parked_state(self):
    """Simulate parked vehicle state"""
    self.send_panda_states(ignition=False)
    self.send_car_state(speed=0.0)
    self.send_device_state(voltage=12.5)

  def simulate_normal_vibrations(self, duration_sec: float = 12.0):
    """Simulate normal parked vibrations for calibration"""
    print(f"Simulating normal vibrations for {duration_sec}s (calibration)...")
    end_time = time.time() + duration_sec
    while time.time() < end_time:
      # Normal gravity with small random noise
      x = np.random.normal(0.0, 0.05)
      y = np.random.normal(0.0, 0.05)
      z = np.random.normal(9.81, 0.1)
      self.send_accelerometer(x, y, z)
      self.simulate_parked_state()
      time.sleep(0.1)  # 10Hz
    print("Calibration phase complete")

  def simulate_shock(self, intensity: float = 5.0):
    """Simulate a shock/impact"""
    print(f"Simulating SHOCK with intensity {intensity}...")
    # Sudden acceleration spike
    self.send_accelerometer(intensity, intensity * 0.5, 9.81 + intensity)
    self.simulate_parked_state()

  def monitor_events(self):
    """Monitor parkingEvent messages"""
    print("Monitoring parking events...")
    while self.running:
      self.sm.update(100)
      if self.sm.updated['parkingEvent']:
        evt = self.sm['parkingEvent']
        state_names = {
          0: 'disabled',
          1: 'calibrating',
          2: 'monitoring',
          3: 'shockDetected',
          4: 'lowBattery',
          5: 'shutdownPending'
        }
        state_name = state_names.get(evt.state, 'unknown')
        print(f"  State: {state_name} | Shock: {evt.shockDetected} | "
              f"Intensity: {evt.shockIntensity:.2f} | "
              f"Battery: {evt.batteryVoltage:.1f}V | "
              f"Calibration: {evt.calibrationProgress*100:.0f}%")

  def run_full_test(self):
    """Run complete test sequence"""
    print("\n" + "="*60)
    print("PARKING MONITOR TEST")
    print("="*60)

    # Enable parking mode
    print("\n1. Enabling ParkingModeEnabled param...")
    self.params.put_bool("ParkingModeEnabled", True)
    time.sleep(0.5)

    # Start event monitor thread
    self.running = True
    monitor_thread = threading.Thread(target=self.monitor_events, daemon=True)
    monitor_thread.start()

    # Phase 1: Simulate parked state and calibration
    print("\n2. Simulating parked state (ignition off, speed=0)...")
    self.simulate_parked_state()
    time.sleep(1)

    # Phase 2: Calibration with normal vibrations
    print("\n3. Calibration phase (10 seconds of normal vibrations)...")
    self.simulate_normal_vibrations(duration_sec=12.0)

    # Phase 3: Continue monitoring
    print("\n4. Monitoring phase (5 seconds)...")
    for _ in range(50):
      x = np.random.normal(0.0, 0.05)
      y = np.random.normal(0.0, 0.05)
      z = np.random.normal(9.81, 0.1)
      self.send_accelerometer(x, y, z)
      self.simulate_parked_state()
      time.sleep(0.1)

    # Phase 4: Simulate shock
    print("\n5. Simulating SHOCK...")
    self.simulate_shock(intensity=8.0)
    time.sleep(2)

    # Check results
    print("\n6. Checking results...")
    last_shock = self.params.get("ParkingModeLastShock")
    if last_shock:
      print(f"   Last shock recorded: {last_shock.decode()}")
    else:
      print("   No shock recorded (threshold may not have been exceeded)")

    shock_alert = self.params.get("Offroad_ParkingShock")
    if shock_alert:
      print(f"   Alert created: {shock_alert.decode()}")
    else:
      print("   No alert created")

    # Cleanup
    self.running = False
    print("\n" + "="*60)
    print("TEST COMPLETE")
    print("="*60 + "\n")

  def test_low_battery_shutdown(self):
    """Test low battery shutdown"""
    print("\n" + "="*60)
    print("LOW BATTERY TEST")
    print("="*60)

    self.params.put_bool("ParkingModeEnabled", True)
    self.simulate_parked_state()

    print("Simulating low battery (11.5V)...")
    self.send_device_state(voltage=11.5)
    time.sleep(2)

    do_shutdown = self.params.get_bool("DoShutdown")
    print(f"DoShutdown param: {do_shutdown}")


def main():
  tester = ParkingMonitorTester()

  print("\nAvailable tests:")
  print("  1. Full test (calibration + shock)")
  print("  2. Low battery test")
  print("  3. Quick shock test (skip calibration)")
  print("  4. Run all tests")

  choice = input("\nSelect test (1-4): ").strip()

  if choice == "1":
    tester.run_full_test()
  elif choice == "2":
    tester.test_low_battery_shutdown()
  elif choice == "3":
    print("Sending shock immediately...")
    tester.params.put_bool("ParkingModeEnabled", True)
    tester.simulate_parked_state()
    tester.simulate_shock(intensity=10.0)
    time.sleep(1)
  elif choice == "4":
    tester.run_full_test()
    tester.test_low_battery_shutdown()
  else:
    print("Running full test by default...")
    tester.run_full_test()


if __name__ == "__main__":
  main()
