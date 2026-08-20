"""Log a 3D robot rollout to nebo's action modality.

Requires the robotics extra::

    uv pip install 'nebo[robotics]'
    uv run python examples/robot_rollout.py

The MJCF is inline, so nothing is downloaded. Two instances share one
scene: the simulated arm, and a "reference" trajectory offset along X —
the same pattern you'd use to compare a policy against a target pose.
"""

import math
import sys

import numpy as np

import nebo as nb

try:
    import mujoco
except ImportError:
    sys.exit(
        "This example needs MuJoCo. Install the extra with:\n"
        "    pip install 'nebo[robotics]'"
    )

from nebo.extras.robotics import mj_pose

# A two-link arm on a ground plane. Visual geoms are contype=0/conaffinity=0
# (MuJoCo Menagerie's convention), so nebo tags them "visual" and keeps the
# thicker collision capsules hidden until you toggle them on in the UI.
MJCF = """
<mujoco model="two_link_arm">
  <option gravity="0 0 -9.81"/>
  <worldbody>
    <light pos="0 0 3"/>
    <geom name="floor" type="plane" size="2 2 0.1" rgba="0.25 0.28 0.32 1"/>
    <body name="upper" pos="0 0 0.6">
      <joint name="shoulder" type="hinge" axis="0 1 0"/>
      <geom name="upper_vis" type="capsule" fromto="0 0 0 0 0 -0.28"
            size="0.035" rgba="0.15 0.55 0.95 1"
            contype="0" conaffinity="0"/>
      <geom name="upper_col" type="capsule" fromto="0 0 0 0 0 -0.28"
            size="0.055"/>
      <body name="lower" pos="0 0 -0.28">
        <joint name="elbow" type="hinge" axis="0 1 0"/>
        <geom name="lower_vis" type="capsule" fromto="0 0 0 0 0 -0.24"
              size="0.028" rgba="0.95 0.45 0.15 1"
              contype="0" conaffinity="0"/>
        <geom name="lower_col" type="capsule" fromto="0 0 0 0 0 -0.24"
              size="0.048"/>
        <body name="hand" pos="0 0 -0.24">
          <geom name="hand_vis" type="sphere" size="0.045"
                rgba="0.95 0.85 0.2 1" contype="0" conaffinity="0"/>
        </body>
      </body>
    </body>
  </worldbody>
  <actuator>
    <!-- Position servos: ctrl is a target joint angle, so the arm tracks
         the sinusoid below instead of sagging under gravity. -->
    <position joint="shoulder" kp="60" kv="4" ctrlrange="-2 2"/>
    <position joint="elbow" kp="40" kv="3" ctrlrange="-2.5 2.5"/>
  </actuator>
</mujoco>
"""

STEPS = 400


@nb.fn()
def build_scene():
    """Compile the arm once and publish it as a body model."""
    model = mujoco.MjModel.from_xml_string(MJCF)
    data = mujoco.MjData(model)
    # Passing the compiled MjModel skips a second compile of the same XML.
    ref = nb.log_body_model("two_link_arm", mjcf=model)
    nb.log_text("setup", f"{len(ref.body_names)} bodies: {', '.join(ref.body_names)}")
    return model, data, ref


@nb.fn()
def rollout(model, data, ref):
    """Drive the arm with a sinusoidal policy and log every frame."""
    for step in range(STEPS):
        phase = step / 40.0
        data.ctrl[0] = 1.2 * math.sin(phase)
        data.ctrl[1] = 1.6 * math.sin(phase * 1.7)
        mujoco.mj_step(model, data)

        poses = mj_pose(model, data)

        # A "reference" instance: the same arm, shifted along X, tracking
        # the target the policy is chasing. Any (N, 7) array works — this
        # one is just the live pose translated.
        reference = poses.copy()
        reference[:, 0] += 0.9

        # Dict form -> two instances in ONE scene, labeled in the UI.
        nb.log_body_transform("arm", ref, {
            "policy": poses,
            "reference": reference,
        }, step=step)

        # poses[-1] is the "hand" body, the last entry in ref.body_names.
        nb.log_line("hand/height", float(poses[-1, 2]), step=step)
        nb.log_line("hand/reach", float(poses[-1, 0]), step=step)
        nb.log_line("ctrl/shoulder", float(data.ctrl[0]), step=step)
        if step % 50 == 0:
            nb.log_text("progress", f"step {step}: hand z={poses[-1, 2]:.3f}", step=step)


def main():
    nb.md(
        "# Two-link arm rollout\n\n"
        "A sinusoidal policy drives a two-link arm. The **Actions** tab "
        "renders both the policy pose and an offset reference pose in one "
        "3D scene — press play in the tracker to watch the episode."
    )
    model, data, ref = build_scene()
    rollout(model, data, ref)
    print(f"logged {STEPS} frames")


if __name__ == "__main__":
    main()
