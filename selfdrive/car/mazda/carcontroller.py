from cereal import car
from opendbc.can.packer import CANPacker
from selfdrive.car import apply_driver_steer_torque_limits, apply_ti_steer_torque_limits
from selfdrive.car.mazda import mazdacan
from selfdrive.car.mazda.values import CarControllerParams, Buttons, GEN1
from common.realtime import ControlsTimer as Timer

VisualAlert = car.CarControl.HUDControl.VisualAlert
LongCtrlState = car.CarControl.Actuators.LongControlState


class CarController:
  def __init__(self, dbc_name, CP, VM):
    self.CP = CP
    self.apply_steer_last = 0
    self.ti_apply_steer_last = 0
    self.packer = CANPacker(dbc_name)
    self.brake_counter = 0
    self.frame = 0
    self.params = CarControllerParams(CP)
    self.hold_timer = Timer(6.0)
    self.hold_delay = Timer(1.0) # delay before we start holding as to not hit the brakes too hard
    self.resume_timer = Timer(0.5)
    self.cancel_delay = Timer(0.07) # 70ms delay to try to avoid a race condition with stock system
      
  def update(self, CC, CS, now_nanos):
    can_sends = []

    apply_steer = 0
    ti_apply_steer = 0

    if CC.latActive:
      # calculate steer and also set limits due to driver torque
      if CS.CP.enableTorqueInterceptor:
        if CS.ti_lkas_allowed:
          ti_new_steer = int(round(CC.actuators.steer * self.params.TI_STEER_MAX))
          ti_apply_steer = apply_ti_steer_torque_limits(ti_new_steer, self.ti_apply_steer_last,
                                                    CS.out.steeringTorque, self.params)

      new_steer = int(round(CC.actuators.steer * self.params.STEER_MAX))
      apply_steer = apply_driver_steer_torque_limits(new_steer, self.apply_steer_last,
                                                     CS.out.steeringTorque, self.params)
    self.apply_steer_last = apply_steer
    self.ti_apply_steer_last = ti_apply_steer
    
    if self.CP.carFingerprint in GEN1:
      if CC.cruiseControl.cancel:
        # If brake is pressed, let us wait >70ms before trying to disable crz to avoid
        # a race condition with the stock system, where the second cancel from openpilot
        # will disable the crz 'main on'. crz ctrl msg runs at 50hz. 70ms allows us to
        # read 3 messages and most likely sync state before we attempt cancel.
        if Timer.interval(10) and not (CS.out.brakePressed and not self.cancel_delay.active()):
          # Cancel Stock ACC if it's enabled while OP is disengaged
          # Send at a rate of 10hz until we sync with stock ACC state
          can_sends.append(mazdacan.create_button_cmd(self.packer, self.CP.carFingerprint, CS.crz_btns_counter, Buttons.CANCEL))
      else:
        self.cancel_delay.reset()
        if CC.cruiseControl.resume and Timer.interval(5):
          # Mazda Stop and Go requires a RES button (or gas) press if the car stops more than 3 seconds
          # Send Resume button when planner wants car to move
          can_sends.append(mazdacan.create_button_cmd(self.packer, self.CP.carFingerprint, CS.crz_btns_counter, Buttons.RESUME))

      # send HUD alerts
      if Timer.interval(50):
        ldw = CC.hudControl.visualAlert == VisualAlert.ldw
        # steer_required = CC.hudControl.visualAlert == VisualAlert.steerRequired
        # TODO: find a way to silence audible warnings so we can add more hud alerts
        #steer_required = steer_required and CS.lkas_allowed_speed
        steer_required = CS.out.steerFaultTemporary
        can_sends.append(mazdacan.create_alert_command(self.packer, CS.cam_laneinfo, ldw, steer_required))
      
      # send steering command if GEN1
      #The ti cannot be detected unless OP sends a can message to it because the ti only transmits when it 
      #sees the signature key in the designated address range.
      can_sends.append(mazdacan.create_ti_steering_control(self.packer, self.CP.carFingerprint,
                                                        self.frame, ti_apply_steer))
    else:
      resume = False
      hold = False
      if Timer.interval(2): # send ACC command at 50hz 
        """
        Without this hold/resum logic, the car will only stop momentarily.
        It will then start creeping forward again. This logic allows the car to
        apply the electric brake to hold the car. The hold delay also fixes a
        bug with the stock ACC where it sometimes will apply the brakes too early 
        when coming to a stop. 
        """
        if CS.out.standstill: # if we're stopped
          if not self.hold_delay.active(): # and we have been stopped for more than hold_delay duration. This prevents a hard brake if we aren't fully stopped.
            if (CC.cruiseControl.resume or CC.cruiseControl.override or (CC.actuators.longControlState == LongCtrlState.starting)): # and we want to resume
              self.resume_timer.reset() # reset the resume timer so its active
            else: # otherwise we're holding
              hold = self.hold_timer.active() # hold for 6s. This allows the electric brake to hold the car.
              
        else: # if we're moving
          self.hold_timer.reset() # reset the hold timer so its active when we stop
          self.hold_delay.reset() # reset the hold delay
          
        resume = self.resume_timer.active() # stay on for 0.5s to release the brake. This allows the car to move.
        can_sends.append(mazdacan.create_acc_cmd(self, self.packer, CS, CC, hold, resume))

    # always send to the stock system
    can_sends.append(mazdacan.create_steering_control(self.packer, self.CP.carFingerprint,
                                                      self.frame, apply_steer, CS.cam_lkas))

    new_actuators = CC.actuators.copy()
    new_actuators.steer = apply_steer / self.params.STEER_MAX
    new_actuators.steerOutputCan = apply_steer

    self.frame += 1
    Timer.tick()
    return new_actuators, can_sends
    

