function
  ----------------------------------------------------------------
  -- MVT-485Pro 称重节点 → Seedling Monitoring
  -- [升级版：支持服务器远程下发置零命令]
  ----------------------------------------------------------------
  local NET  = 1
  local UART = 2
  local CMD_WEIGHT = "01030000000305CB"
  
  -- [新增] 远程下发置零命令 (对应 0x0006 临时去皮)
  local CMD_ZERO   = "010600060001A80B" 

  local seq = 0
  local decimals = 1
  local imei = ""
  local iccid = ""
  local lng = nil
  local lat = nil
  local lbsAt = 0
  local locationStatus = "no_valid_lbs"

  local function call(fn)
    local ok,a,b = pcall(fn)
    if ok then return a,b end
    return nil,nil
  end

  local function clearUart()
    while UartGetRecChAndDel(UART) ~= nil do end
  end

  local function query(cmd,header,length)
    clearUart()
    UartSetSendCh(UART, string.fromHex(cmd))
    local h = ""
    for _ = 1,30 do
      sys.wait(50)
      local x = UartGetRecChAndDel(UART)
      if x then
        h = h .. string.upper(string.toHex(x))
        local p = string.find(h, header, 1, true)
        if p and #h >= p + length - 1 then
          return h,p
        end
      end
    end
    return nil,nil
  end

  -- [新增] 执行置零
  local function doRemoteZero()
    log.info("mvt485", "Executing remote zero...")
    query(CMD_ZERO, "01060006", 16)
  end

  local function readWeight()
    local h,p = query(CMD_WEIGHT, "010306", 22)
    if not h or not p then return nil end

    local high = tonumber(string.sub(h, p + 6, p + 9), 16)
    local low  = tonumber(string.sub(h, p + 10, p + 13), 16)
    local status = tonumber(string.sub(h, p + 14, p + 17), 16)

    if not high or not low or not status then return nil end

    local raw = high * 65536 + low
    if raw >= 2147483648 then raw = raw - 4294967296 end

    local weight = raw / (10 ^ decimals)

    local stable = (status % 2) == 1
    local overload = (math.floor(status / 2) % 2) == 1
    local zero = (math.floor(status / 4) % 2) == 1
    local negative = (math.floor(status / 8) % 2) == 1

    return {
      raw = raw, weight = weight, stable = stable, 
      overload = overload, zero = zero, negative = negative
    }
  end

  local function addDeviceInfo(d)
    d.battery_mv = tonumber(call(function() return PerGetVbattV() end))
    d.csq = tonumber(call(function() return mobile.csq() end))
    d.rsrp = call(function() if mobile.rsrp then return mobile.rsrp() end end)
    d.rsrq = call(function() if mobile.rsrq then return mobile.rsrq() end end)
    d.snr = call(function() if mobile.snr then return mobile.snr() end end)
    
    local now = os.time()
    if lbsAt == 0 or now - lbsAt >= 60 then
      lbsAt = now
      local x,y = call(function() return GetLbs() end)
      x = tonumber(x)
      y = tonumber(y)
      if x and y and x ~= 0 and y ~= 0 and x >= -180 and x <= 180 and y >= -90 and y <= 90 then
        lng, lat, locationStatus = x, y, "ok"
      elseif lng and lat then
        locationStatus = "cached"
      else
        locationStatus = "no_valid_lbs"
      end
    end
    d.longitude = lng; d.latitude = lat; d.location_status = locationStatus
  end

  local function report()
    local r = readWeight()
    seq = seq + 1
    if not r then
      log.info("mvt485", "read timeout")
      return
    end
    if r.overload then
      log.info("mvt485", "overload")
      return
    end

    local d = {
      task_version = "mvt485_weight_v2_remote_zero",
      imei = imei, iccid = iccid,
      timestamp = os.time(), report_sequence = seq, report_interval_s = 10,
      weight_g = r.weight, weight_raw = r.raw, decimal_places = decimals,
      stable = r.stable, zero = r.zero, negative = r.negative,
      overload = false, measurement_valid = true
    }
    
    addDeviceInfo(d)
    
    if PronetGetNetSta(NET) == 1 then
      PronetSetSendCh(NET, json.encode(d))
    end
  end

  -- [新增] 处理服务器下发的消息
  local function processDownlink()
    local msg = call(function() return PronetGetRecChAndDel(NET) end)
    if msg then
      -- 如果收到服务器发送的特定的置零指令 (比如 "CMD:ZERO")，就执行置零
      if string.find(msg, "CMD:ZERO") then
         doRemoteZero()
      end
    end
  end

  ----------------------------------------------------------------
  -- 初始化
  ----------------------------------------------------------------
  sys.wait(5000)
  PronetStopProRecCh(NET)
  UartStopProRecCh(1)
  PerSetDo(1,1)
  sys.wait(5000)
  
  imei = tostring(call(function() return mobile.imei() end) or "")
  iccid = tostring(call(function() return PerGetIccid() end) or "")

  while true do
    -- 1. 处理下行数据 (远程命令)
    processDownlink()
    
    -- 2. 上报称重数据
    local ok,err = pcall(report)
    if not ok then log.info("mvt485", err) end

    sys.wait(10000)
  end
end
