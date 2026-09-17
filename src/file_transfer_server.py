# -*- coding: utf-8 -*-
"""
内网文件互传服务器（零依赖，仅用 Python 标准库）

功能:
    * iPad / 手机 -> 电脑:  浏览器选文件上传, 存到电脑的「接收的文件」文件夹
    * 电脑 -> iPad / 手机:  浏览器选文件(或拖拽)上传, iPad 打开同一网址即可下载
    * 单个文件支持 20G 及以上
    * 多分片并发上传 + 断点续传: 网络中断后自动从断点继续, 服务器重启也能接着传
    * 大文件下载支持断点续传(HTTP Range)
    * 实时速率显示(瞬时/峰值/平均)

用法:
    python src/file_transfer_server.py  (或双击「启动服务器.bat」)

    然后在 iPad / 手机 / 其他电脑的浏览器打开启动时显示的地址。

目录结构:
    启动服务器.bat                入口，双击即可启动
    src/file_transfer_server.py   全部源码（单文件，含内嵌网页）
    接收的文件/                   上传的文件落在这里
    .uploads_tmp/                 未完成的分片，断点续传用；删掉只是无法续传
    server.log                    运行日志

性能说明:
    传输速度主要受限于 Wi-Fi 实际带宽、iPad 无线芯片与存储读取速度。
    本程序已采用「多分片并发 + 乱序落盘 + 大块读写 + TCP_NODELAY」来尽量跑满带宽,
    并发的分片数可在页面顶部按需调整(iPad 默认 2, 电脑默认 2)。

iPad 上传中途卡死的修复(v3.2):
    现象: 传了一小段就停住, 进度条不动、也不报错, 连心跳请求都一起消失。
    定位: 服务端日志显示分片已完整收下并返回 200, 但 iPad 侧 XHR 最终只拿到
          status=0(网络层失败) —— 响应根本没回到浏览器。
    根因: iPad/Safari 会复用上一次的 HTTP 长连接发下一个分片, 这条连接一旦被
          Wi-Fi 抖动 / NAT 悄悄断掉, 请求发得出去、服务端也收得到, 但响应永远
          回不来; 而客户端的重试逻辑又依赖 XHR 回调, 于是整条链路静默死掉。
    措施:
      1. 所有接口响应一律 Connection: close, 每个分片走全新连接(服务端)
      2. 先把分片读成 ArrayBuffer 再发送, 避免 WebKit 回读超大文件时无回调(客户端)
      3. 新增整任务级看门狗: 一段时间没有分片真正完成就掐断全部在途请求并重建
         会话续传, 而不是干等(客户端)
      4. 所有接口请求带超时, 避免 iPad 上 fetch 永久挂起导致重试永不触发(客户端)
      5. iPad 自动使用 2MB 分片, 减小卡死后重传的代价并降低内存压力
      6. 埋点批量上报, 避免请求数暴涨挤占连接
      7. 支持 chunked 编码的请求体, 并把连接空闲超时从 300s 缩短到 120s(服务端)

v3.3 底层重构：新增 WebSocket 上传通道（默认），HTTP 作为兜底
    上面那些措施都是在"每个分片一次 HTTP 请求"的框架里打补丁，天花板很明显：
    每片都要一次 TCP 握手、一次拥塞窗口从零爬升，也都是一次潜在的卡死点。
    WebSocket 全程只用一条连接：握手一次，之后窗口一直保持满速，分片在连接上
    连续流出，客户端按流水线深度并行发送，既没有连接抖动可卡，也不会让 Wi-Fi
    射频因为突发空闲而降速。协议极简（open / {"i":n}+二进制帧 / ack / finish），
    HTTP 分片接口原样保留，WebSocket 不可用时自动回退。

v3.4：多条 WebSocket 并行（页面「连接数」可调，默认 2）
    一条 TCP 流的吞吐 ≈ 拥塞窗口 ÷ 往返时延，Wi-Fi 上单流常被卡在 10~30 MB/s，
    远没吃满带宽。现在开 1~4 条长连接共享同一个待发队列，谁快谁多领。
    这不等于回到"每片一连接"的老路：老问题是**每片都要新建连接**（9.25GB 约 2300 次），
    现在是**整份文件只建 1~4 次**，触发卡死的机会少了约三个数量级；
    而且 WebSocket 断线一定有 onclose/onerror，不像当年 XHR 那样静默不回调。
    防退化设计：
      * 共享队列 + 拉取式分配，慢连接自然少领活
      * 每条连接独立看门狗（阈值比全局宽松），只掐那一条并单独重连
      * 全局看门狗兜底：整体停滞才整批重建
      * 反复停滞自动减少连接数，一路退到 1 条（即 v3.3 已验证可用的形态），
        并把降级后的值写回页面下拉，用户看得见
      * 心跳走 WS 自身，不额外占用 HTTP 连接；连接数封顶 4，给 Safari 的 6 条留余量
"""

import os
import re
import sys
import json
import time
import socket
import struct
import base64
import hashlib
import shutil
import datetime
import threading
import webbrowser
from urllib.parse import urlparse, parse_qs, quote
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# 控制台编码兜底：避免在 cmd(GBK) 下输出中文/特殊符号时报错
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# ==================== 配置区（按需修改） ====================
VERSION = "3.4.2"                    # 程序版本(页面会校验, 不一致时自动刷新)
PORT = 8899                          # 服务端口
AUTO_OPEN_BROWSER = True             # 启动时在本机自动打开浏览器
SOCKET_TIMEOUT = 120                 # 单次网络读写超时(秒)
                                     # iPad 上传时若连接被 Wi-Fi/NAT 悄悄断开, 服务端会一直
                                     # 干等; 缩短超时可以尽快回收死连接, 让客户端重连续传
TMP_KEEP_DAYS = 7                    # 未完成分片/完成记录保留天数(重启后可继续续传)

CHUNK_SIZE = 4 * 1024 * 1024         # 分片大小(4MB, 客户端按此对齐)
                                     # iPad/Safari 读取大文件(尤其 iCloud 文件)时,
                                     # 小分片更稳、卡住后重传的代价也更小
READ_BLOCK = 1024 * 1024             # 服务端单次读写块大小(1MB)
DEFAULT_PARALLEL = 2                 # 页面默认并发分片数(iPad 上并发过高反而更容易卡)

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"   # WebSocket 握手固定 GUID
WS_TIMEOUT = 600                     # WebSocket 连接空闲超时(秒)
WS_MAX_FRAME = 64 * 1024 * 1024      # 单帧上限, 防止异常客户端打爆内存

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# 源码放在 src/ 子目录里时，工程根目录是它的上一级
if os.path.basename(BASE_DIR).lower() == "src":
    BASE_DIR = os.path.dirname(BASE_DIR)

SAVE_DIR = os.path.join(BASE_DIR, "接收的文件")       # 文件最终保存位置
TMP_DIR = os.path.join(BASE_DIR, ".uploads_tmp")      # 分片临时目录(隐藏)
LOG_FILE = os.path.join(BASE_DIR, "server.log")       # 请求日志(排查问题用)
LOG_LOCK = threading.Lock()                           # 日志写入锁(多线程并发写会串行错乱)
# ============================================================


HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="文件互传">
<title>内网文件互传</title>
<style>
*{margin:0;padding:0;box-sizing:border-box;-webkit-tap-highlight-color:transparent}
body{font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif;
     min-height:100vh;background:linear-gradient(165deg,#e0e7ff,#f5f3ff 50%,#fce7f3);
     color:#1e293b;padding:max(18px,env(safe-area-inset-top)) 16px max(40px,env(safe-area-inset-bottom))}
.wrap{max-width:600px;margin:0 auto}
h1{font-size:21px;text-align:center;margin:6px 0 4px;font-weight:700}

.card{background:rgba(255,255,255,.9);border-radius:24px;padding:20px 16px;
      box-shadow:0 18px 50px rgba(79,70,229,.12)}
.zone{border:2px dashed #c7d2fe;border-radius:18px;background:#f8faff;padding:30px 16px;
      text-align:center;cursor:pointer;transition:all .18s}
.zone:active{transform:scale(.98);background:#eef2ff}
.zone.over{background:#eef2ff;border-color:#818cf8;transform:scale(1.01)}
.zone .ico{width:58px;height:58px;margin:0 auto 12px;border-radius:50%;
           background:linear-gradient(135deg,#6366f1,#a855f7);
           display:flex;align-items:center;justify-content:center;
           box-shadow:0 10px 24px rgba(99,102,241,.35)}
.zone .ico svg{width:27px;height:27px}
.zone b{font-size:17px;display:block;margin-bottom:6px}
.zone span{font-size:12.5px;color:#94a3b8;line-height:1.7;display:block}
.par{display:flex;align-items:center;justify-content:center;gap:8px;margin-top:14px;
     font-size:12.5px;color:#64748b}
.par select{border:1px solid #e2e8f0;background:#fff;border-radius:9px;padding:5px 8px;
            font-size:12.5px;color:#1e293b}
.task{background:#fff;border-radius:16px;padding:13px 14px;margin-top:10px;
      box-shadow:0 2px 12px rgba(15,23,42,.06)}
.trow{display:flex;justify-content:space-between;gap:10px;align-items:flex-start}
.tname{font-size:14px;font-weight:600;word-break:break-all;flex:1;min-width:0}
.tsize{color:#94a3b8;font-weight:400;font-size:12px;white-space:nowrap}
.bar{height:8px;border-radius:4px;background:#e2e8f0;margin-top:11px;overflow:hidden}
.bar i{display:block;height:100%;width:0;border-radius:4px;
       background:linear-gradient(90deg,#6366f1,#a855f7);transition:width .2s linear}
.task.ok .bar i{background:linear-gradient(90deg,#10b981,#34d399)}
.task.bad .bar i{background:linear-gradient(90deg,#ef4444,#f87171)}
.spd{display:flex;align-items:baseline;gap:6px;margin-top:9px}
.spd b{font-size:22px;font-weight:700;color:#4f46e5;
       font-variant-numeric:tabular-nums;letter-spacing:-.5px}
.spd b em{font-style:normal;font-size:12px;font-weight:600;color:#818cf8;margin-left:2px}
.spd s{text-decoration:none;font-size:11.5px;color:#94a3b8;margin-left:auto;
       white-space:nowrap;font-variant-numeric:tabular-nums}
.task.ok .spd b{color:#059669}
.task.bad .spd b{color:#dc2626}
.stat{font-size:12px;color:#64748b;margin-top:5px;
      font-variant-numeric:tabular-nums;word-break:break-all}
.foot{display:flex;justify-content:space-between;align-items:center;margin-top:9px;gap:10px}
.foot span{font-size:11.5px;color:#94a3b8}
.tcancel{border:none;background:#f1f5f9;color:#64748b;border-radius:8px;
         padding:5px 11px;font-size:12px;cursor:pointer}
.files{margin-top:22px}
.fh{display:flex;justify-content:space-between;align-items:center;margin:0 4px 8px}
.fh h2{font-size:16px}
.fh .tools{display:flex;gap:4px;align-items:center}
.fh button{border:none;background:none;color:#6366f1;font-size:13px;cursor:pointer;padding:8px 6px}
.disk{font-size:12px;color:#94a3b8}
.file{display:flex;align-items:center;gap:10px;background:#fff;border-radius:16px;
      padding:12px 14px;margin-top:8px;box-shadow:0 2px 12px rgba(15,23,42,.06)}
.fnm{flex:1;min-width:0}
.fnm b{font-size:14px;font-weight:600;word-break:break-all;line-height:1.4}
.fsz{color:#94a3b8;font-size:12px;margin-top:4px}
.btn{border:none;border-radius:10px;padding:9px 13px;font-size:13px;cursor:pointer;
     text-decoration:none;white-space:nowrap;display:inline-block}
.dl{background:#eef2ff;color:#4f46e5;font-weight:600}
.del{background:#fee2e2;color:#dc2626}
.empty{text-align:center;color:#94a3b8;font-size:13px;padding:22px 0}
.toast{position:fixed;left:50%;bottom:36px;transform:translateX(-50%) translateY(20px);
       background:rgba(15,23,42,.93);color:#fff;padding:12px 22px;border-radius:14px;
       font-size:14px;opacity:0;pointer-events:none;transition:all .25s;
       max-width:86vw;text-align:center;line-height:1.5;z-index:99}
.toast.show{opacity:1;transform:translateX(-50%) translateY(0)}
input[type=file]{display:none}
.tip{font-size:12px;color:#94a3b8;text-align:center;margin-top:14px;line-height:1.7}
</style>
</head>
<body>
<div class="wrap">
  <h1>内网文件互传</h1>

  <div class="card">
    <div class="zone" id="zone">
      <div class="ico">
        <svg viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="2.2"
             stroke-linecap="round" stroke-linejoin="round">
          <path d="M12 19V5M5 12l7-7 7 7"/>
        </svg>
      </div>
      <b>点击选择文件</b>
      <span>电脑上也可直接把文件拖到这里<br>支持任意类型 · 可多选 · 支持超大文件</span>
    </div>
    <div class="par">
      <label>连接数</label>
      <select id="parallel">
        <option value="1">1（最稳）</option>
        <option value="2" selected>2</option>
        <option value="3">3</option>
        <option value="4">4</option>
      </select>
      <label>停滞判定</label>
      <select id="wdog">
        <option value="2">2 秒</option>
        <option value="3">3 秒</option>
        <option value="5">5 秒</option>
        <option value="10">10 秒</option>
        <option value="25">25 秒</option>
      </select>
    </div>
    <div id="tasks"></div>
  </div>

  <div class="files">
    <div class="fh">
      <h2>电脑上的文件</h2>
      <div class="tools">
        <span class="disk" id="disk"></span>
        <button onclick="refresh()">刷新</button>
      </div>
    </div>
    <div id="list"><p class="empty">加载中…</p></div>
  </div>

  <p class="tip">
    上传的文件保存在电脑的「接收的文件」文件夹<br>
    传输大文件时请保持亮屏并停留在本页面<br>
    <span style="opacity:.65">页面版本 <b id="ver"></b></span>
  </p>
</div>

<div class="toast" id="toast"></div>
<input type="file" id="picker" multiple>

<script>
var PAGE_VER = "__VERSION__";   /* 页面版本, 与服务端比对 */

/* iPad / iPhone 用的是 WebKit：大文件 + 长连接复用时容易"静默卡死"，
   这里对 iOS 单独采用更小的分片和更激进的停滞自愈策略 */
var UA = navigator.userAgent || '';
var IS_IOS = /iPad|iPhone|iPod/.test(UA) ||
             (navigator.platform === 'MacIntel' && (navigator.maxTouchPoints || 0) > 1);

var CHUNK = IS_IOS ? (2 * 1024 * 1024) : (4 * 1024 * 1024);  /* 新会话时向服务端申请 */
var MAX_RETRY = 4;                        /* 单个分片最大重试次数 */
var WDOG_DEFAULT = IS_IOS ? 2 : 10;       /* 停滞判定秒数，页面顶部可随时改 */

/* 停滞判定时间（秒）：读页面上的选择，运行中改也立即生效 */
function wdogSec(){
  var el = document.getElementById('wdog');
  var v = el ? parseInt(el.value, 10) : 0;
  return (v > 0) ? v : WDOG_DEFAULT;
}

var $ = function(id){ return document.getElementById(id); };
var zone = $('zone');
var picker = $('picker');

/* ---------------- 选择文件 ---------------- */
function pick(){ picker.click(); }
zone.addEventListener('click', pick);
picker.addEventListener('change', function(){
  var fs = Array.prototype.slice.call(this.files);
  this.value = '';
  if (fs.length) fs.forEach(startUpload);
});

/* ---------------- 电脑端拖拽 ---------------- */
['dragover','dragenter'].forEach(function(ev){
  zone.addEventListener(ev, function(e){ e.preventDefault(); zone.classList.add('over'); });
});
zone.addEventListener('dragleave', function(){ zone.classList.remove('over'); });
zone.addEventListener('drop', function(e){
  e.preventDefault();
  zone.classList.remove('over');
  var fs = Array.prototype.slice.call(e.dataTransfer.files);
  if (fs.length) fs.forEach(startUpload);
});
window.addEventListener('dragover', function(e){ e.preventDefault(); });
window.addEventListener('drop', function(e){ e.preventDefault(); });

/* ---------------- 工具函数 ---------------- */
function fmtSize(n){
  if (n === undefined || n === null) return '-';
  if (n < 1024) return n + ' B';
  if (n < 1048576) return (n/1024).toFixed(1) + ' KB';
  if (n < 1073741824) return (n/1048576).toFixed(1) + ' MB';
  return (n/1073741824).toFixed(2) + ' GB';
}
function fmtSpeed(bps){
  if (!bps || bps < 1) return '0';
  if (bps < 1048576) return (bps/1024).toFixed(0);
  return (bps/1048576).toFixed(1);
}
function speedUnit(bps){
  if (!bps || bps < 1048576) return 'KB/s';
  return 'MB/s';
}
function fmtTime(sec){
  if (!isFinite(sec) || sec <= 0) return '--';
  var m = Math.floor(sec/60), s = Math.round(sec%60);
  if (m < 60) return m + ':' + (s<10?'0':'') + s;
  return Math.floor(m/60) + ':' + ((m%60)<10?'0':'') + (m%60) + ':' + (s<10?'0':'') + s;
}
function esc(s){
  return String(s).replace(/[&<>"']/g, function(c){
    return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];
  });
}
function toast(msg){
  var t = $('toast');
  t.textContent = msg;
  t.classList.add('show');
  clearTimeout(t._h);
  t._h = setTimeout(function(){ t.classList.remove('show'); }, 2600);
}
function sleep(ms){ return new Promise(function(r){ setTimeout(r, ms); }); }
/* 所有接口请求都带超时：iPad 上 fetch 可能永久挂着既不 resolve 也不 reject，
   那样重试逻辑永远等不到触发，表现为"卡死无报错" */
function _fetchT(url, method, ms){
  var opt = { method: method || 'GET' };
  var t = null;
  if (typeof AbortController !== 'undefined'){
    var ac = new AbortController();
    opt.signal = ac.signal;
    t = setTimeout(function(){ try{ ac.abort(); }catch(e){} }, ms);
  }
  return fetch(url, opt).then(function(r){
    if (t) clearTimeout(t);
    if (!r.ok) throw new Error('HTTP ' + r.status);
    return r.json();
  }, function(e){
    if (t) clearTimeout(t);
    throw new Error('请求超时');
  });
}
function jget(url){ return _fetchT(url, 'GET', 20000); }
function jpost(url){ return _fetchT(url, 'POST', 30000); }
/* 埋点：批量上报到服务端日志，便于排查 iPad 端卡住的位置。
   注意绝不能每个事件都发一条请求 —— iPad 上请求数暴涨会挤占连接，反而加剧卡死 */
var _trq = [], _trAt = 0;
function flushTrace(){
  _trAt = Date.now();
  if (!_trq.length) return;
  var payload = _trq.join(' | ').slice(0, 900);
  _trq = [];
  try {
    var url = '/api/trace?msg=' + encodeURIComponent(payload);
    if (navigator.sendBeacon){ navigator.sendBeacon(url); }
    else { fetch(url, { method:'POST' }).catch(function(){}); }
  } catch (e){}
}
function trace(msg){
  try {
    _trq.push(msg);
    if (_trq.length > 60) _trq.shift();
    if (Date.now() - _trAt > 3000) flushTrace();
  } catch (e){}
}

/* ---------------- 任务卡片 ---------------- */
function createTask(file){
  var el = document.createElement('div');
  el.className = 'task';
  el.innerHTML =
    '<div class="trow">' +
      '<div class="tname">' + esc(file.name) + '</div>' +
      '<div class="tsize">' + fmtSize(file.size) + '</div>' +
    '</div>' +
    '<div class="bar"><i></i></div>' +
    '<div class="spd"><b>0<em>MB/s</em></b><s>峰值 0 MB/s</s></div>' +
    '<div class="stat">准备中…</div>' +
    '<div class="foot"><span class="extra"></span>' +
    '<button class="tcancel">取消</button></div>';
  $('tasks').insertBefore(el, $('tasks').firstChild);

  var obj = {
    el: el,
    bar: el.querySelector('.bar i'),
    spd: el.querySelector('.spd b'),
    peak: el.querySelector('.spd s'),
    stat: el.querySelector('.stat'),
    extra: el.querySelector('.extra'),
    cancelled: false,
    finished: false,
    aborts: [],
    total: file.size,
    chunk: CHUNK,
    doneChunks: 0,
    totalChunks: 0,
    inflight: {},          /* index -> 已发送字节 */
    peakSpeed: 0,
    samples: [],           /* [时间, 字节] 滑动窗口 */
    smooth: 0,
    parallel: 1,
    sending: 0,            /* 真正发出、还在等响应的请求数 */
    stalled: false,        /* 被看门狗判定为卡死，需要重建会话 */
    stallCount: 0,
    lastCommitAt: Date.now(),
    lastProgressAt: Date.now()
  };
  el.querySelector('.tcancel').addEventListener('click', function(){
    obj.cancelled = true;
    obj.finished = true;
    obj.stat.textContent = '正在取消…';
    obj.aborts.forEach(function(a){ try{ a(); }catch(e){} });
  });

  return obj;
}

/* 已完成字节 = 已确认分片 * 分片大小 + 在途分片已发送量 */
function sentBytes(task){
  var sum = task.doneChunks * task.chunk;
  for (var k in task.inflight) sum += task.inflight[k];
  return Math.min(sum, task.total);
}

function paint(task){
  var done = sentBytes(task);
  var pct = task.total ? (done / task.total * 100) : 0;
  task.bar.style.width = pct.toFixed(2) + '%';
  task.lastProgressAt = Date.now();

  var now = Date.now();
  task.samples.push([now, done]);
  /* 只保留最近 3 秒的采样点 */
  while (task.samples.length > 2 && now - task.samples[0][0] > 3000){
    task.samples.shift();
  }
  var speed = 0;
  if (task.samples.length >= 2){
    var t0 = task.samples[0], t1 = task.samples[task.samples.length-1];
    var dt = (t1[0] - t0[0]) / 1000;
    if (dt > 0.25) speed = (t1[1] - t0[1]) / dt;
  }
  if (speed > 0) task.smooth = task.smooth ? task.smooth*0.7 + speed*0.3 : speed;
  var cur = task.smooth;
  if (cur > task.peakSpeed) task.peakSpeed = cur;

  task.spd.innerHTML = fmtSpeed(cur) + '<em>' + speedUnit(cur) + '</em>';
  task.peak.textContent = '峰值 ' + fmtSpeed(task.peakSpeed) + ' ' + speedUnit(task.peakSpeed);

  var eta = cur > 1024 ? (task.total - done) / cur : Infinity;
  task.stat.textContent = pct.toFixed(1) + '% · ' + fmtSize(done) + ' / ' + fmtSize(task.total) +
      ' · 剩余 ' + fmtTime(eta);
  task.extra.textContent = task.doneChunks + '/' + task.totalChunks + ' 片' +
      (task.transport ? ' · ' + task.transport : '');
}

function taskDone(task, savedName){
  task.finished = true;
  task.el.classList.add('ok');
  task.bar.style.width = '100%';
  task.spd.innerHTML = fmtSpeed(task.peakSpeed) + '<em>' + speedUnit(task.peakSpeed) + '</em>';
  task.peak.textContent = '峰值 ' + fmtSpeed(task.peakSpeed) + ' ' + speedUnit(task.peakSpeed);
  task.stat.textContent = '已完成 · ' + savedName;
  task.extra.textContent = '平均 ' + fmtSpeed(task.avgSpeed || 0) + ' ' +
      speedUnit(task.avgSpeed || 0);
  var btn = task.el.querySelector('.tcancel');
  if (btn) btn.remove();
  refresh();
}

function taskFail(task, msg){
  task.finished = true;
  task.el.classList.add('bad');
  task.stat.textContent = '失败：' + msg;
  var btn = task.el.querySelector('.tcancel');
  if (btn) btn.remove();
}

/* ---------------- 读取并发送单个分片 ---------------- */
/* iPad/WebKit 直接把大文件的 Blob 交给 XHR 时，需要网络层回过头去读磁盘；
   在超大文件上偶尔读不出来、也不回调任何事件，表现就是"卡住且不报错"。
   这里先把分片读成 ArrayBuffer（小块、行为确定），再发送。 */
function readSlice(task, index){
  var offset = index * task.chunk;
  var end = Math.min(offset + task.chunk, task.total);
  var blob = task.file.slice(offset, end);
  if (blob && typeof blob.arrayBuffer === 'function'){
    return blob.arrayBuffer();
  }
  return new Promise(function(res, rej){
    var fr = new FileReader();
    fr.onload  = function(){ res(fr.result); };
    fr.onerror = function(){ rej(new Error('读取文件失败')); };
    fr.readAsArrayBuffer(blob);
  });
}

function sendChunk(task, uid, index, onProgress){
  var xhr = null;
  var ab = function(){ try{ if (xhr) xhr.abort(); } catch(e){} };
  task.aborts.push(ab);
  function drop(){
    var i = task.aborts.indexOf(ab);
    if (i >= 0) task.aborts.splice(i, 1);
  }

  return readSlice(task, index).then(function(buf){
    if (task.cancelled || task.stalled){ drop(); throw new Error('已取消'); }

    return new Promise(function(resolve, reject){
      xhr = new XMLHttpRequest();
      xhr.open('POST', '/api/part?uploadId=' + encodeURIComponent(uid) +
                       '&index=' + index, true);
      xhr.timeout = 120000;

      var lastPaint = 0;
      xhr.upload.onprogress = function(e){
        if (e.lengthComputable){
          task.inflight[index] = e.loaded;
          var t = Date.now();
          /* 节流：进度事件极其频繁，iPad 上频繁操作 DOM 会拖死主线程，
             导致分片发完后浏览器迟迟不处理响应，表现为"卡住" */
          if (t - lastPaint > 300){ lastPaint = t; onProgress(); }
        }
      };

      /* 分片级停滞监测：该分片若干秒毫无进展就掐掉重发，
         不影响其它分片，也不会误杀慢速传输。
         阈值跟随页面的"停滞判定"，略小于它，好让单片重发先于整任务重建生效 */
      var chunkLimit = Math.max(3, Math.round(wdogSec() * 0.8));
      var lastLoaded = -1, ticks = 0;
      var timer = setInterval(function(){
        if (!xhr || xhr.readyState === 4){ clearInterval(timer); return; }
        var cur = task.inflight[index];
        if (cur === undefined || cur === lastLoaded){
          if (++ticks >= chunkLimit){
            clearInterval(timer);
            trace('chunk stall idx=' + index + ' rs=' + xhr.readyState +
                  ' loaded=' + (cur === undefined ? -1 : cur));
            try { xhr.abort(); } catch(e){}
          }
        } else { lastLoaded = cur; ticks = 0; }
      }, 1000);

      function done(){
        clearInterval(timer);
        drop();
        task.sending = Math.max(0, (task.sending || 0) - 1);
      }
      xhr.onload = function(){
        done();
        if (xhr.status === 200){
          try { resolve(JSON.parse(xhr.responseText)); }
          catch (e){ reject(new Error('返回数据异常')); }
        } else { reject(new Error('HTTP ' + xhr.status)); }
      };
      xhr.onerror = function(){
        done();
        trace('xhr error idx=' + index + ' rs=' + xhr.readyState);
        reject(new Error('网络中断'));
      };
      xhr.ontimeout = function(){ done(); reject(new Error('分片超时')); };
      xhr.onabort   = function(){ done(); reject(new Error('已取消')); };

      task.inflight[index] = 0;
      task.sending = (task.sending || 0) + 1;
      xhr.send(buf);
    });
  }).catch(function(e){ drop(); throw e; });
}

/* ---------------- 看门狗：整条链路级停滞自愈 ---------------- */
/* 之前 iPad 卡死时，单片重试器、心跳、后续请求会一起消失 —— 没有任何报错。
   这里用一个独立定时器盯住"有没有分片真正完成"，超时就抢救。

   分两级，绝大多数情况只走便宜的一级：
     一级：掐断在途分片，让 worker 用原会话立刻重发（不重建会话，零额外开销）
     二级：每 3 次才重建一次会话（要重新 init + 拉整张位图，代价大）
   另外：只有"确实有请求卡在网络上"才判定卡死，正在读盘不算，
   这样把阈值压到 2 秒也不会因为 iPad 读 iCloud 文件慢而误判。 */
function startWatchdog(task){
  if (task.wdTimer) return;
  task.wdTimer = setInterval(function(){
    if (task.finished || task.cancelled){ clearInterval(task.wdTimer); return; }

    /* 没有请求在途：大家都在读盘/切片，不是网络卡死，重置计时 */
    if (!task.sending){ task.lastCommitAt = Date.now(); return; }

    var idle = Date.now() - (task.lastCommitAt || 0);
    if (idle < wdogSec() * 1000) return;

    task.stallCount = (task.stallCount || 0) + 1;
    var rebuild = (task.stallCount % 3 === 0);
    trace('TASK STALL idle=' + Math.round(idle / 1000) + 's count=' + task.stallCount +
          ' done=' + task.doneChunks + '/' + task.totalChunks +
          ' inflight=' + task.sending + ' rebuild=' + rebuild);

    if (rebuild) task.stalled = true;      /* 让 worker 退出，上层重建会话 */

    /* 掐断在途分片：一级恢复时 worker 会自动重发同一批分片 */
    task.aborts.slice().forEach(function(a){ try{ a(); }catch(e){} });
    if (rebuild && task._stallReject){
      var r = task._stallReject;
      task._stallReject = null;
      r(new Error('STALL'));
    }
    task.lastCommitAt = Date.now();
    task.stat.textContent = rebuild
      ? '停滞较久，重建会话续传…'
      : '停滞 ' + Math.round(idle / 1000) + ' 秒，重发中…';

    /* 反复停滞说明页面已经不稳定，重载一次自愈（有次数保护，不会无限刷） */
    if (task.stallCount >= 12 && !sessionStorage.getItem('iu_reload')){
      try { sessionStorage.setItem('iu_reload', '1'); } catch(e){}
      flushTrace();
      setTimeout(function(){ location.reload(); }, 600);
    }
  }, 500);   /* 检查频率要高于最小阈值，2 秒档位才准 */
}

/* ---------------- 主流程：并发上传 ---------------- */
async function runUpload(file, task){
  var started = Date.now();
  var attempt = 0;
  var lastDone = -1;
  startWatchdog(task);

  while (true){
    try {
      task.stalled = false;
      task.aborts = [];

      var init = await jpost('/api/init?name=' + encodeURIComponent(file.name) +
                             '&size=' + file.size +
                             '&mtime=' + (file.lastModified || 0) +
                             '&chunk=' + CHUNK);
      if (!init.ok) throw new Error(init.msg || '初始化失败');
      var uid = init.uploadId;

      if (init.done){
        taskDone(task, init.savedName || file.name);
        toast('该文件已在电脑上，无需重传');
        return;
      }

      task.chunk = init.chunk || CHUNK;
      task.totalChunks = init.total;
      task.doneChunks = init.received.length;
      task.parallel = parseInt($('parallel').value, 10) || 1;
      task.lastCommitAt = Date.now();
      task.stalled = false;      /* init 期间若被看门狗误判，这里复位，避免空转一轮 */
      trace('init ok size=' + file.size + ' chunk=' + task.chunk +
            ' total=' + init.total + ' done=' + task.doneChunks +
            ' parallel=' + task.parallel + ' attempt=' + attempt);

      if (attempt > 0 && task.doneChunks > 0){
        task.stat.textContent = '已从断点继续（' + fmtSize(task.doneChunks * task.chunk) + '）';
      }
      task.samples = [];
      paint(task);

      /* 待发送队列：跳过服务端已有分片 */
      var got = {};
      init.received.forEach(function(i){ got[i] = 1; });
      var queue = [];
      for (var i = 0; i < init.total; i++){
        if (!got[i]) queue.push(i);
      }

      var qi = 0;
      var fatal = null;
      var self = task;

      async function worker(){
        while (true){
          if (self.cancelled || self.stalled || fatal) return;
          var my = qi++;
          if (my >= queue.length) return;
          var idx = queue[my];

          var okThis = false;
          for (var r = 0; r < MAX_RETRY; r++){
            if (self.cancelled || self.stalled || fatal) return;
            try {
              var res = await sendChunk(self, uid, idx, function(){ paint(self); });
              delete self.inflight[idx];
              if (res && res.error){
                /* 服务端表示该分片已存在或偏移异常，视作完成 */
                if (res.dup){ okThis = true; break; }
                throw new Error(res.msg || '分片被拒绝');
              }
              self.doneChunks++;
              self.lastCommitAt = Date.now();   /* 看门狗据此判断链路是否还活着 */
              self.stallCount = 0;              /* 有进展就清零，只有连续停滞才升级处理 */
              okThis = true;
              paint(self);
              break;
            } catch (e){
              delete self.inflight[idx];
              trace('part fail idx=' + idx + ' try=' + r + ' err=' + e.message);
              if (self.cancelled || self.stalled) return;
              if (r === MAX_RETRY - 1){ fatal = e; return; }
              await sleep(200 * (r + 1));   /* 退避要短，卡死后尽快把分片补回去 */
            }
          }
          if (!okThis) return;
        }
      }

      var pool = [];
      for (var w = 0; w < task.parallel; w++) pool.push(worker());
      trace('workers started=' + task.parallel + ' queue=' + queue.length);

      /* 与看门狗赛跑：链路被判定卡死时立刻跳出，不用干等 XHR 超时 */
      var stallP = new Promise(function(_, rej){ task._stallReject = rej; });
      await Promise.race([Promise.all(pool), stallP]);
      task._stallReject = null;
      if (task.stalled) throw new Error('STALL');

      if (task.cancelled){
        await jpost('/api/cancel?uploadId=' + encodeURIComponent(uid)).catch(function(){});
        task.stat.textContent = '已取消';
        return;
      }
      if (fatal) throw fatal;

      var fin = await jpost('/api/finish?uploadId=' + encodeURIComponent(uid));
      if (!fin.ok) throw new Error(fin.msg || '合并失败');

      task.finished = true;
      if (task.wdTimer) clearInterval(task.wdTimer);
      var secs = (Date.now() - started) / 1000;
      task.avgSpeed = secs > 0 ? file.size / secs : 0;
      taskDone(task, fin.name);
      toast('上传完成：' + fin.name + '，平均 ' +
            fmtSpeed(task.avgSpeed) + ' ' + speedUnit(task.avgSpeed));
      return;

    } catch (err){
      attempt++;
      trace('runUpload error attempt=' + attempt + ' msg=' + err.message);
      if (task.cancelled){ task.stat.textContent = '已取消'; return; }

      /* 只要有实质进展就重置重试计数：长文件允许反复自愈，不轻易判失败 */
      if (task.doneChunks > lastDone){ lastDone = task.doneChunks; attempt = 0; }

      if (err.message === 'HTTP 404'){
        /* 页面与服务端接口对不上：自动重新加载一次，尽量自愈 */
        taskFail(task, '正在重新加载页面…');
        if (!sessionStorage.getItem('iu_404_reload')){
          try { sessionStorage.setItem('iu_404_reload', '1'); } catch(e){}
          setTimeout(function(){ location.reload(); }, 700);
        } else {
          taskFail(task, '接口不存在，请彻底关闭本页面后重新打开');
        }
        return;
      }
      if (attempt > 12){
        taskFail(task, err.message);
        return;
      }
      task.stat.textContent = '连接中断，正在重连续传…';
      flushTrace();
      await sleep(500);
    }
  }
}

/* ==================== WebSocket 上传通道（首选） ====================
   与 HTTP 通道的本质区别：
     HTTP  —— 每个分片一次完整请求：一次 TCP 握手 + 拥塞窗口从零爬升 + 一次可能的卡死
     WS    —— 整份文件只用一个连接：握手一次，之后窗口一直保持满速，
              分片在同一条连接上连续流出，既没有连接抖动可卡，也不会让 Wi-Fi 因空闲降速
   协议极简：
     客户端 -> {"cmd":"open",...}      开会话，服务端回已完成分片列表
     客户端 -> {"i":123} + 二进制帧    一个分片
     服务端 -> {"ok":true,"i":123,...} 确认
     客户端 -> {"cmd":"finish"}        收尾合并
   ===================================================================== */
var WS_CHUNK = 4 * 1024 * 1024;      /* WS 通道分片：单连接下大分片开销更低 */
var WS_MAX_BUFFER = 10 * 1024 * 1024; /* 每条连接的背压上限 */

/* 连接数（= 并行 TCP 流数）。单流吞吐被"窗口÷往返时延"卡住，
   多开几条就能把 Wi-Fi 带宽吃满；但要给 Safari 的 6 条上限留余量 */
function connCount(){
  var el = document.getElementById('parallel');
  var v = el ? parseInt(el.value, 10) : 0;
  return Math.max(1, Math.min(4, v || 2));
}

function wsURL(){
  var p = (location.protocol === 'https:') ? 'wss:' : 'ws:';
  return p + '//' + location.host + '/ws';
}

function wsConnect(){
  return new Promise(function(resolve, reject){
    var ws;
    try { ws = new WebSocket(wsURL()); }
    catch (e){ reject(new Error('不支持 WebSocket')); return; }
    var t = setTimeout(function(){
      try { ws.close(); } catch(e){}
      reject(new Error('WS 连接超时'));
    }, 10000);
    ws.binaryType = 'arraybuffer';
    ws.onopen  = function(){ clearTimeout(t); resolve(ws); };
    ws.onerror = function(){ clearTimeout(t); reject(new Error('WS 连接失败')); };
    ws.onclose = function(){ clearTimeout(t); reject(new Error('WS 已关闭')); };
  });
}

/* 一次 WS 会话：nConn 条连接共享一个待发队列，谁快谁多领（拉取式，不做静态均分） */
function wsSession(file, task, nConn){
  return new Promise(function(resolve, reject){
    var closed = false, finishing = false, everOpened = false;
    var built = false;               /* 本会话是否已建好队列；重建会话时必须重新建 */
    var queue = [], qi = 0, conns = [], reconnects = 0;
    var depth = (nConn <= 2) ? 2 : 1;      /* 每条连接上的流水线深度 */
    var wdTimer = null, hbTimer = null;

    /* 单条连接卡死的判定比全局宽松：它还在慢慢挪的时候不该被误杀 */
    var connLimit = function(){ return Math.max(6000, wdogSec() * 3000); };
    var allLimit  = function(){ return Math.max(3000, wdogSec() * 1500); };

    function bye(err){
      if (closed) return;
      closed = true;
      clearInterval(wdTimer); clearInterval(hbTimer);
      conns.forEach(function(c){ try{ if (c.ws) c.ws.close(); } catch(e){} });
      reject(err);
    }

    function requeue(idx){ queue.push(idx); }

    function inflightOf(c){ return Object.keys(c.sent).length; }
    function inflightAll(){
      var n = 0;
      conns.forEach(function(c){ if (!c.dead) n += inflightOf(c); });
      return n;
    }
    function liveConns(){
      return conns.filter(function(c){ return !c.dead && c.ws && c.ws.readyState === 1; });
    }

    /* 队列排空且所有连接都空手 -> 收尾 */
    function maybeFinish(){
      if (closed || finishing || !built) return;
      if (qi < queue.length) return;
      if (inflightAll() > 0) return;
      var live = liveConns();
      if (!live.length) return;
      finishing = true;
      try { live[0].ws.send(JSON.stringify({cmd: 'finish'})); }
      catch (e){ bye(e); }
    }

    function onAck(c, m){
      delete c.sent[m.i];
      c.lastAck = Date.now();
      if (m.ok){
        task.doneChunks++;
        task.lastCommitAt = Date.now();
        task.stallCount = 0;
        paint(task);
      } else {
        trace('ws reject idx=' + m.i + ' ' + (m.msg || ''));
        requeue(m.i);
      }
      pump(c);
      maybeFinish();
    }

    function onFinish(m){
      if (!m.ok){ bye(new Error(m.msg || '合并失败')); return; }
      task.finished = true;
      closed = true;
      clearInterval(wdTimer); clearInterval(hbTimer);
      var secs = (Date.now() - task._t0) / 1000;
      task.avgSpeed = secs > 0 ? file.size / secs : 0;
      conns.forEach(function(c){ try{ if (c.ws) c.ws.close(); } catch(e){} });
      resolve(m);
    }

    /* 某条连接挂了：把它手上没确认的分片还回队列，再单独补一条 */
    function onDead(c, why){
      if (closed || c.dead) return;
      c.dead = true;
      trace('ws conn#' + c.id + ' dead: ' + why +
            ' done=' + task.doneChunks + '/' + task.totalChunks);
      Object.keys(c.sent).forEach(function(k){ requeue(parseInt(k, 10)); });
      c.sent = {};
      try { if (c.ws) c.ws.close(); } catch(e){}

      if (finishing){ bye(new Error('收尾时连接中断')); return; }
      if (!everOpened){                       /* 一条都没连上过 -> 整条路有问题 */
        if (conns.every(function(x){ return x.dead; })) bye(new Error('WS 连接失败'));
        return;
      }
      if (reconnects++ > 30){ bye(new Error('WS 反复断开')); return; }

      setTimeout(function(){
        if (closed) return;
        conns[c.id] = makeConn(c.id);
      }, 300);
    }

    function makeConn(id){
      var c = { id: id, ws: null, sent: {}, dead: false, pumping: false,
                lastAck: Date.now(), opened: false };
      wsConnect().then(function(sock){
        if (closed){ try{ sock.close(); } catch(e){} return; }
        c.ws = sock;
        c.lastAck = Date.now();
        sock.binaryType = 'arraybuffer';
        sock.onclose  = function(){ onDead(c, 'close'); };
        sock.onerror  = function(){ onDead(c, 'error'); };
        sock.onmessage = function(ev){
          var m;
          try { m = JSON.parse(ev.data); } catch(e){ return; }
          if (m.cmd === 'open'){ onOpen(c, m); return; }
          if (m.cmd === 'finish'){ onFinish(m); return; }
          if (m.cmd === 'pong'){ c.lastAck = Date.now(); task.lastCommitAt = Date.now(); return; }
          if (m.i !== undefined){ onAck(c, m); return; }
        };
        try {
          sock.send(JSON.stringify({cmd: 'open', name: file.name, size: file.size,
                                    mtime: file.lastModified || 0, chunk: WS_CHUNK}));
        } catch (e){ onDead(c, 'send'); }
      }).catch(function(){ onDead(c, 'connect'); });
      return c;
    }

    function onOpen(c, m){
      if (closed) return;
      if (!m.ok){ bye(new Error(m.msg || '创建会话失败')); return; }
      c.opened = true;
      c.lastAck = Date.now();
      everOpened = true;

      if (!built){                     /* 用第一条连接的响应建队列，其余复用同一会话 */
        built = true;
        task.chunk = m.chunk || WS_CHUNK;
        task.totalChunks = m.total;
        task.lastCommitAt = Date.now();
        task.parallel = nConn;
        trace('ws open conns=' + nConn + ' chunk=' + task.chunk +
              ' total=' + m.total + ' done=' + (m.received || []).length);
        if (m.done){
          task.finished = true;
          closed = true;
          clearInterval(wdTimer); clearInterval(hbTimer);
          conns.forEach(function(x){ try{ if (x.ws) x.ws.close(); } catch(e){} });
          resolve(m);
          return;
        }
        task.doneChunks = (m.received || []).length;
        var got = {};
        (m.received || []).forEach(function(i){ got[i] = 1; });
        for (var i = 0; i < m.total; i++) if (!got[i]) queue.push(i);
        paint(task);
      }
      pump(c);
      if (conns.filter(function(x){ return x.opened; }).length >= nConn){
        conns.forEach(function(x){ if (x.opened) pump(x); });
      }
    }

    /* 流水式发送：本条连接在途不超过 depth，且受缓冲上限约束 */
    async function pump(c){
      if (c.pumping || c.dead) return;
      c.pumping = true;
      try {
        while (!closed && !c.dead && c.ws && c.ws.readyState === 1){
          if (inflightOf(c) >= depth) break;
          if (c.ws.bufferedAmount > WS_MAX_BUFFER) break;
          var idx = (qi < queue.length) ? queue[qi++] : null;
          if (idx === null){ maybeFinish(); break; }

          var buf;
          try {
            buf = await readSlice(task, idx);
          } catch (e){
            c.fails = (c.fails || 0) + 1;
            trace('ws read fail idx=' + idx + ' n=' + c.fails);
            requeue(idx);
            if (c.fails > 5){ bye(new Error('读取文件失败')); return; }
            continue;
          }
          if (closed || c.dead){ requeue(idx); return; }
          try {
            c.sent[idx] = Date.now();
            c.fails = 0;
            c.ws.send(JSON.stringify({i: idx}));
            c.ws.send(buf);
          } catch (e){
            delete c.sent[idx];
            requeue(idx);
            onDead(c, 'send');
            return;
          }
        }
      } finally {
        c.pumping = false;
      }
    }

    /* 看门狗：先按条判（只掐那一条），再全局兜底（整批重建） */
    wdTimer = setInterval(function(){
      if (closed) return;
      conns.forEach(function(c){
        if (c.dead || !c.opened) return;
        if (!inflightOf(c)){ c.lastAck = Date.now(); return; }
        if (Date.now() - c.lastAck > connLimit()){
          trace('conn#' + c.id + ' stall ' +
                Math.round((Date.now() - c.lastAck) / 1000) + 's');
          onDead(c, 'stall');
        }
      });
      var busy = conns.some(function(c){ return !c.dead && inflightOf(c); });
      if (!busy){ task.lastCommitAt = Date.now(); return; }
      if (Date.now() - task.lastCommitAt < allLimit()) return;
      task.stallCount++;
      trace('WS STALL(all) idle=' +
            Math.round((Date.now() - task.lastCommitAt) / 1000) + 's' +
            ' done=' + task.doneChunks + '/' + task.totalChunks);
      task.stat.textContent = '整体停滞，重建连接…';
      bye(new Error('STALL'));
    }, 500);

    /* 心跳走 WS 自身，不再额外占用 HTTP 连接 */
    hbTimer = setInterval(function(){
      if (closed) return;
      conns.forEach(function(c){
        if (c.dead || !c.ws || c.ws.readyState !== 1) return;
        try { c.ws.send(JSON.stringify({cmd: 'ping', t: Date.now()})); } catch(e){}
      });
    }, 15000);

    for (var k = 0; k < nConn; k++) conns.push(makeConn(k));
  });
}

/* WS 通道总控：卡死就重建续传；反复卡死自动减少连接数，一路退到 1 条（已验证可用的形态） */
async function wsRunUpload(file, task){
  var attempt = 0, lastDone = -1, stallEvents = 0;
  var n = connCount();
  task.conns = n;
  task._t0 = Date.now();
  while (true){
    try {
      var r = await wsSession(file, task, n);
      if (task.cancelled){ task.stat.textContent = '已取消'; return; }
      if (task.doneChunks > lastDone) lastDone = task.doneChunks;
      var nm = r.savedName || r.name || file.name;
      taskDone(task, nm);
      if (r.done){
        toast('该文件已在电脑上，无需重传');
      } else {
        toast('上传完成：' + nm + '，平均 ' +
              fmtSpeed(task.avgSpeed) + ' ' + speedUnit(task.avgSpeed));
      }
      return;
    } catch (e){
      attempt++;
      trace('ws session end attempt=' + attempt + ' err=' + e.message);
      if (task.cancelled || task.finished) return;
      if (task.doneChunks > lastDone){ lastDone = task.doneChunks; attempt = 0; }

      if (e.message === 'STALL'){
        stallEvents++;
        if (stallEvents >= 3 && n > 1){
          n -= 1;
          task.conns = n;
          stallEvents = 0;
          trace('degrade conns -> ' + n);
          try {
            $('parallel').value = String(n);
            localStorage.setItem('iu_parallel', String(n));
          } catch (err){}
          task.stat.textContent = '已自动减少到 ' + n + ' 条连接以稳定传输';
        }
      }
      if (attempt > 10) throw e;
      flushTrace();
      await sleep(600);
    }
  }
}

function startUpload(file){
  var task = createTask(file);
  task.file = file;
  if (typeof WebSocket === 'undefined'){
    task.transport = 'HTTP';
    runUpload(file, task);
    return;
  }
  task.transport = 'WS×' + connCount();
  wsRunUpload(file, task).catch(function(e){
    if (task.cancelled || task.finished) return;
    trace('WS 通道不可用，回退 HTTP：' + e.message);
    task.transport = 'HTTP（回退）';
    task.samples = [];
    task.stalled = false;
    task.aborts = [];
    runUpload(file, task);
  });
}

/* ---------------- 文件列表 ---------------- */
function refresh(){
  jget('/api/files').then(function(data){
    var box = $('list');
    if (!data.files.length){
      box.innerHTML = '<p class="empty">还没有文件</p>';
      return;
    }
    box.innerHTML = data.files.map(function(f){
      return '<div class="file">' +
        '<div class="fnm"><b>' + esc(f.name) + '</b>' +
        '<div class="fsz">' + fmtSize(f.size) + ' · ' + esc(f.time) + '</div></div>' +
        '<a class="btn dl" href="/download?name=' + encodeURIComponent(f.name) +
        '" download="' + esc(f.name) + '">下载</a>' +
        '<button class="btn del" data-name="' + encodeURIComponent(f.name) + '">删除</button>' +
        '</div>';
    }).join('');
    Array.prototype.forEach.call(box.querySelectorAll('.del'), function(b){
      b.addEventListener('click', function(){ delFile(this.getAttribute('data-name')); });
    });
  }).catch(function(){
    $('list').innerHTML = '<p class="empty">无法连接服务器</p>';
  });
}

function delFile(encName){
  if (!confirm('确定删除该文件？删除后不可恢复。')) return;
  jpost('/api/delete?name=' + encName).then(function(d){
    toast(d.ok ? '已删除' : (d.msg || '删除失败'));
    refresh();
  }).catch(function(){ toast('删除失败'); });
}

function loadDisk(){
  jget('/api/status').then(function(d){
    if (d.ok) $('disk').textContent = '剩余 ' + fmtSize(d.free);
  }).catch(function(){});
}

/* ---------------- 防止 iPad 长时间上传时息屏 ---------------- */
var wakeLock = null;
function requestWakeLock(){
  try {
    if (navigator.wakeLock && navigator.wakeLock.request){
      navigator.wakeLock.request('screen').then(function(l){ wakeLock = l; }).catch(function(){});
    }
  } catch (e){}
}
document.addEventListener('visibilitychange', function(){
  if (document.visibilityState === 'visible') requestWakeLock();
});
requestWakeLock();

/* 默认参数、分片提示，以及记住手动选择的档位 */
(function(){
  var pSave = null, wSave = null;
  try {
    pSave = localStorage.getItem('iu_parallel');
    wSave = localStorage.getItem('iu_wdog');
  } catch(e){}

  $('wdog').value = wSave || String(WDOG_DEFAULT);
  if (pSave){
    $('parallel').value = pSave;
  } else if (IS_IOS){
    $('parallel').value = '2';
  }

  $('parallel').addEventListener('change', function(){
    try { localStorage.setItem('iu_parallel', this.value); } catch(e){}
  });
  $('wdog').addEventListener('change', function(){
    try { localStorage.setItem('iu_wdog', this.value); } catch(e){}
  });

})();

/* 页面被切走/隐藏时把埋点发出去，便于排查卡死位置 */
window.addEventListener('pagehide', flushTrace);
document.addEventListener('visibilitychange', function(){
  if (document.visibilityState === 'hidden') flushTrace();
});

/* 版本自检：若页面来自旧缓存，自动刷新一次，避免接口对不上 */
$('ver').textContent = PAGE_VER;
(function(){
  fetch('/api/version').then(function(r){ return r.json(); }).then(function(d){
    if (d && d.version && d.version !== PAGE_VER){
      if (!sessionStorage.getItem('trae_ver_reload')){
        try { sessionStorage.setItem('trae_ver_reload', '1'); } catch(e){}
        location.reload();
      }
    } else {
      try { sessionStorage.removeItem('trae_ver_reload'); } catch(e){}
    }
  }).catch(function(){});
})();

refresh();
loadDisk();
setInterval(loadDisk, 30000);
</script>
</body>
</html>"""


# ==================================================================
#                            服务端实现
# ==================================================================

def get_lan_ips():
    """获取本机所有局域网 IPv4 地址"""
    ips = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except Exception:
        pass
    try:  # 兜底：通过 UDP 探测默认路由网卡
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ips.add(s.getsockname()[0])
        s.close()
    except Exception:
        pass
    return sorted(ip for ip in ips if not ip.startswith("127."))


def sanitize_filename(name):
    """清理文件名，防止路径穿越与非法字符"""
    name = (name or "").replace("\\", "_").replace("/", "_")
    name = re.sub(r'[\x00-\x1f<>:"|?*]', "_", name)
    name = name.strip().strip(".").strip()
    return name[:150] if name else ""


def unique_path(directory, filename):
    """同名文件自动追加 (1) (2)，避免覆盖"""
    base, ext = os.path.splitext(filename)
    candidate = os.path.join(directory, filename)
    i = 1
    while os.path.exists(candidate):
        candidate = os.path.join(directory, "%s(%d)%s" % (base, i, ext))
        i += 1
    return candidate


def make_upload_id(name, size, mtime):
    """由 文件名+大小+修改时间 生成稳定的上传 ID，便于断点续传"""
    raw = "%s|%s|%s" % (name, size, mtime)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:24]


def _count_done(bmpath):
    """统计位图里已完成的分片数（每片一个字节，值为 0 或 1）"""
    try:
        with open(bmpath, "rb") as f:
            return f.read().count(b"\x01")
    except Exception:
        return 0


def chunk_count(size, chunk=None):
    if not size or size <= 0:
        return 0
    chunk = chunk or CHUNK_SIZE
    return (size + chunk - 1) // chunk


class TransferServer(ThreadingHTTPServer):
    daemon_threads = True
    # Windows 下若为 True，多个进程可同时绑定同一端口，导致请求被随机分发到
    # 旧实例，出现莫名其妙的 404。这里关闭，并在启动时主动检测端口占用。
    allow_reuse_address = False
    request_queue_size = 128        # 允许更多并发连接排队


class FileHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "IntranetTransfer/3.0"
    timeout = SOCKET_TIMEOUT

    # ------------------------- 连接层优化 -------------------------
    def setup(self):
        BaseHTTPRequestHandler.setup(self)
        try:
            # 关闭 Nagle 算法，避免小包延迟累积，提升吞吐
            self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except Exception:
            pass

    # ------------------------- 基础工具 -------------------------
    def _json(self, code, obj, close=True):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        if close:
            # 关键修复：API 一律不用 keep-alive。
            # iPad/Safari 会复用上一次的长连接发下一个分片，一旦这条连接被 Wi-Fi
            # 抖动 / NAT 悄悄断掉，请求能发出去、服务端也能收全并回 200，
            # 但响应永远回不到 iPad —— 客户端只能等到超时拿到 status=0，
            # 表现为"传了一小段就卡死、没有任何报错"。
            # 每个请求都用新连接，可以从根本上消除这种"静默死连接"。
            self.send_header("Connection", "close")
            self.close_connection = True
        self.end_headers()
        self.wfile.write(data)

    def _log_trace(self, msg):
        """把客户端埋点写入日志文件，便于排查浏览器端行为"""
        if not msg:
            return
        try:
            stamp = datetime.datetime.now().strftime("%m-%d %H:%M:%S")
            line = "[%s] %s  TRACE %s" % (stamp, self.client_address[0], msg)
            with LOG_LOCK:
                with open(LOG_FILE, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
            print("  " + line)
        except Exception:
            pass

    def _safe_saved_file(self, raw_name):
        """返回 SAVE_DIR 内的安全文件路径；非法返回 None"""
        name = sanitize_filename(raw_name)
        if not name:
            return None
        path = os.path.join(SAVE_DIR, name)
        if os.path.dirname(os.path.abspath(path)) != os.path.abspath(SAVE_DIR):
            return None
        return path

    def _paths(self, uid):
        """返回 (part, meta, bitmap) 三个路径；uid 非法时返回 (None,None,None)"""
        if not uid or not re.fullmatch(r"[0-9a-f]{24}", uid):
            return None, None, None
        return (os.path.join(TMP_DIR, uid + ".part"),
                os.path.join(TMP_DIR, uid + ".json"),
                os.path.join(TMP_DIR, uid + ".map"))

    def _read_bitmap(self, bmpath, total):
        """读取分片完成位图，返回已完成的 index 列表"""
        try:
            with open(bmpath, "rb") as f:
                data = f.read(total)
        except Exception:
            return []
        return [i for i, b in enumerate(data) if b]

    def _mark_bitmap(self, bmpath, index):
        """标记某个分片已完成（独立句柄，多线程并发安全）"""
        with open(bmpath, "r+b") as f:
            f.seek(index)
            f.write(b"\x01")

    def _recv_to_file(self, fp, offset, length):
        """把请求体精确写入文件的指定偏移，返回写入字节数"""
        fp.seek(offset)
        remaining = length
        buf = bytearray(READ_BLOCK)
        view = memoryview(buf)
        while remaining > 0:
            n = self.rfile.readinto(view[:min(READ_BLOCK, remaining)])
            if not n:
                break
            fp.write(view[:n])
            remaining -= n
        return length - remaining

    def _recv_chunked_to_file(self, fp, offset):
        """读取 chunked 编码的请求体并写入文件指定偏移，返回写入字节数"""
        fp.seek(offset)
        buf = bytearray(READ_BLOCK)
        view = memoryview(buf)
        total = 0
        while True:
            line = self.rfile.readline(65537)
            if not line:
                break
            try:
                csize = int(line.split(b";")[0].strip() or b"0", 16)
            except ValueError:
                break
            if csize == 0:
                self.rfile.readline(65537)          # 结束块后的空行
                break
            remaining = csize
            while remaining > 0:
                n = self.rfile.readinto(view[:min(READ_BLOCK, remaining)])
                if not n:
                    break
                fp.write(view[:n])
                remaining -= n
                total += n
            if remaining:
                break
            self.rfile.readline(65537)              # 每个数据块后的 CRLF
        return total

    def _discard_body(self):
        """错误响应前把请求体读掉，避免破坏 keep-alive"""
        try:
            remaining = int(self.headers.get("Content-Length") or 0)
            buf = bytearray(min(READ_BLOCK, max(remaining, 1)))
            view = memoryview(buf)
            while remaining > 0:
                n = self.rfile.readinto(view[:min(len(buf), remaining)])
                if not n:
                    break
                remaining -= n
        except Exception:
            self.close_connection = True

    # ------------------------- GET -------------------------
    # ==================== WebSocket 上传通道 ====================
    # 为什么要有这条路：
    #   HTTP 每传一个分片就要一次完整请求 —— 一次 TCP 握手、一次拥塞窗口从零爬升，
    #   同时也是一次潜在的卡死点。iPad 上"传几片就卡"正是卡在这里。
    #   WebSocket 全程只用一个连接，握手一次、拥塞窗口爬满后一直保持，
    #   既去掉了每次的连接开销，也让 Wi-Fi 射频不会因为突发空闲而降速。

    def _ws_unmask(self, data, mask):
        """WebSocket 客户端帧必须加掩码。逐字节异或在 Python 里太慢，
           这里用大整数异或（C 实现），4MB 分片只需几毫秒。"""
        if not data or not mask:
            return data
        n = len(data)
        m = (mask * (n // 4 + 1))[:n]
        x = int.from_bytes(data, "big") ^ int.from_bytes(m, "big")
        return x.to_bytes(n, "big")

    def _ws_read_exact(self, n):
        buf = bytearray()
        while len(buf) < n:
            chunk = self.rfile.read(n - len(buf))
            if not chunk:
                return None
            buf += chunk
        return bytes(buf)

    def _ws_recv(self):
        """读取一帧，返回 (opcode, payload)，连接关闭返回 None"""
        head = self._ws_read_exact(2)
        if head is None:
            return None
        b1, b2 = head[0], head[1]
        opcode = b1 & 0x0F
        masked = b2 & 0x80
        ln = b2 & 0x7F
        if ln == 126:
            d = self._ws_read_exact(2)
            if d is None:
                return None
            ln = struct.unpack(">H", d)[0]
        elif ln == 127:
            d = self._ws_read_exact(8)
            if d is None:
                return None
            ln = struct.unpack(">Q", d)[0]
        if ln > WS_MAX_FRAME:
            return None
        mask = self._ws_read_exact(4) if masked else None
        if masked and mask is None:
            return None
        data = self._ws_read_exact(ln) if ln else b""
        if data is None:
            return None
        if masked:
            data = self._ws_unmask(data, mask)
        return opcode, data

    def _ws_send(self, opcode, payload):
        """发送一帧（服务端→客户端不加掩码）"""
        data = payload or b""
        n = len(data)
        hdr = bytearray()
        hdr.append(0x80 | opcode)
        if n < 126:
            hdr.append(n)
        elif n < 65536:
            hdr.append(126)
            hdr += struct.pack(">H", n)
        else:
            hdr.append(127)
            hdr += struct.pack(">Q", n)
        try:
            self.wfile.write(bytes(hdr) + data)
        except Exception:
            raise

    def _ws_json(self, obj):
        try:
            self._ws_send(0x1, json.dumps(obj, ensure_ascii=False).encode("utf-8"))
        except Exception:
            raise

    def _ws_serve(self):
        """处理 /ws 升级并进入消息循环"""
        if (self.headers.get("Upgrade") or "").lower().find("websocket") < 0:
            self._json(400, {"ok": False, "msg": "需要 WebSocket 升级"})
            return
        key = self.headers.get("Sec-WebSocket-Key") or ""
        if not key:
            self._json(400, {"ok": False, "msg": "缺少 Sec-WebSocket-Key"})
            return

        accept = base64.b64encode(
            hashlib.sha1((key + WS_GUID).encode("utf-8")).digest()
        ).decode("ascii")

        self.send_response(101, "Switching Protocols")
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept)
        self.end_headers()
        try:
            self.wfile.flush()
        except Exception:
            pass
        self.close_connection = True
        try:
            self.connection.settimeout(WS_TIMEOUT)
        except Exception:
            pass

        sess = {"uid": None, "part": None, "meta": None, "bmp": None,
                "size": 0, "chunk": CHUNK_SIZE, "total": 0, "pending": None}

        try:
            while True:
                msg = self._ws_recv()
                if msg is None:
                    break
                opcode, data = msg
                if opcode == 0x8:                     # close
                    try:
                        self._ws_send(0x8, b"")
                    except Exception:
                        pass
                    break
                if opcode == 0x9:                     # ping -> pong
                    self._ws_send(0xA, data)
                    continue
                if opcode == 0xA:                     # pong
                    continue
                if opcode == 0x1:                     # text: 命令
                    try:
                        cmd = json.loads(data.decode("utf-8"))
                    except Exception:
                        continue
                    if not self._ws_cmd(sess, cmd):
                        break
                elif opcode == 0x2:                   # binary: 分片数据
                    self._ws_data(sess, data)
        except Exception:
            pass
        finally:
            self.close_connection = True

    def _ws_cmd(self, sess, cmd):
        """处理一条文本命令，返回 False 表示结束连接"""
        op = cmd.get("cmd")

        if op == "open":
            name = sanitize_filename(cmd.get("name", ""))
            if not name:
                self._ws_json({"ok": False, "msg": "缺少文件名"})
                return True
            try:
                size = int(cmd.get("size", 0))
            except (TypeError, ValueError):
                size = 0
            try:
                req_chunk = int(cmd.get("chunk", 0))
            except (TypeError, ValueError):
                req_chunk = 0

            s = self._prepare_session(name, size, str(cmd.get("mtime", "0")), req_chunk)
            sess.update({"uid": s["uid"], "part": s["part"], "meta": s["meta"],
                         "bmp": s["bmp"], "size": s["size"], "chunk": s["chunk"],
                         "total": s["total"], "pending": None})
            if s["done"]:
                self._ws_json({"ok": True, "cmd": "open", "uploadId": s["uid"],
                               "chunk": s["chunk"], "total": s["total"],
                               "received": list(range(s["total"])),
                               "size": size, "done": True,
                               "savedName": s.get("savedName", "")})
            else:
                self._ws_json({"ok": True, "cmd": "open", "uploadId": s["uid"],
                               "chunk": s["chunk"], "total": s["total"],
                               "received": s["received"], "size": size, "done": False})
            return True

        if op == "finish":
            r = self._finish_session(sess.get("uid") or "")
            r["cmd"] = "finish"
            self._ws_json(r)
            return False

        if op == "cancel":
            uid = sess.get("uid")
            if uid:
                part, meta, bmpath = self._paths(uid)
                for p in (part, meta, bmpath):
                    try:
                        if p and os.path.isfile(p):
                            os.remove(p)
                    except OSError:
                        pass
            self._ws_json({"ok": True, "cmd": "cancel"})
            return False

        if op == "ping":
            self._ws_json({"ok": True, "cmd": "pong", "t": cmd.get("t")})
            return True

        # 无 cmd 但带 i：声明下一个二进制帧的分片序号（省掉一次数据拷贝）
        if "i" in cmd:
            try:
                sess["pending"] = int(cmd["i"])
            except (TypeError, ValueError):
                sess["pending"] = None
        return True

    def _ws_data(self, sess, data):
        idx = sess.get("pending")
        sess["pending"] = None
        part, bmpath = sess.get("part"), sess.get("bmp")
        if idx is None or not part or not os.path.isfile(part):
            self._ws_json({"ok": False, "error": "no_session", "msg": "会话已失效"})
            return

        chunk = sess["chunk"]
        size = sess["size"]
        total = sess["total"]
        if idx < 0 or idx >= total:
            self._ws_json({"ok": False, "i": idx, "error": "bad_index",
                           "msg": "分片序号越界"})
            return

        offset = idx * chunk
        try:
            with open(part, "r+b") as f:
                f.seek(offset)
                f.write(data)
        except Exception as e:
            self._ws_json({"ok": False, "i": idx, "msg": "写入失败: %s" % e})
            return

        try:
            self._mark_bitmap(bmpath, idx)
        except Exception as e:
            self._ws_json({"ok": False, "i": idx, "msg": "位图写入失败: %s" % e})
            return

        self._ws_json({"ok": True, "i": idx, "done": _count_done(bmpath),
                       "total": total})

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path in ("/", "/index.html"):
            # 注入版本号，并强力禁止缓存，避免页面与服务端版本错配
            data = HTML.replace("__VERSION__", VERSION).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control",
                             "no-store, no-cache, must-revalidate, max-age=0")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
            self.end_headers()
            self.wfile.write(data)

        elif path == "/ws":
            # 全程只用一个连接上传，彻底摆脱"每片一次 HTTP 请求"带来的握手开销与卡死点
            self._ws_serve()

        elif path == "/api/files":
            files = []
            try:
                for name in os.listdir(SAVE_DIR):
                    fp = os.path.join(SAVE_DIR, name)
                    if os.path.isfile(fp):
                        mt = datetime.datetime.fromtimestamp(os.path.getmtime(fp))
                        files.append({
                            "name": name,
                            "size": os.path.getsize(fp),
                            "time": mt.strftime("%m-%d %H:%M"),
                            "_m": os.path.getmtime(fp),
                        })
                files.sort(key=lambda f: f["_m"], reverse=True)
                for f in files:
                    f.pop("_m", None)
            except Exception:
                pass
            self._json(200, {"ok": True, "files": files})

        elif path == "/api/version":
            self._json(200, {"ok": True, "version": VERSION})

        elif path == "/api/probe":
            # 客户端心跳：只用来确认链路还活着，不写日志，避免刷屏
            self._json(200, {"ok": True, "t": int(time.time())})

        elif path == "/api/init":
            # 兼容：历史版本页面用 GET 调用初始化接口
            self._api_init(parse_qs(parsed.query))

        elif path == "/api/status":
            try:
                usage = shutil.disk_usage(SAVE_DIR)
                self._json(200, {"ok": True, "free": usage.free,
                                 "total": usage.total, "dir": SAVE_DIR})
            except Exception as e:
                self._json(500, {"ok": False, "msg": str(e)})

        elif path == "/download":
            self._do_download(parse_qs(parsed.query).get("name", [""])[0])

        else:
            self._json(404, {"ok": False, "msg": "not found", "version": VERSION,
                             "hint": "接口不存在，页面版本可能过旧，请刷新页面"})

    def _do_download(self, raw_name):
        path = self._safe_saved_file(raw_name)
        if not path or not os.path.isfile(path):
            self._json(404, {"ok": False, "msg": "文件不存在"})
            return

        name = os.path.basename(path)
        size = os.path.getsize(path)
        start, end = 0, size - 1
        status = 200

        rng = self.headers.get("Range")
        if rng and rng.startswith("bytes="):
            spec = rng[6:].split(",")[0].strip()
            try:
                a, _, b = spec.partition("-")
                if a == "":                      # 后缀范围: bytes=-500
                    start = max(0, size - int(b))
                else:
                    start = int(a)
                    if b:
                        end = min(int(b), size - 1)
                if start >= size or start > end:
                    self.send_response(416)
                    self.send_header("Content-Range", "bytes */%d" % size)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                status = 206
            except ValueError:
                start, end, status = 0, size - 1, 200

        length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Disposition",
                         "attachment; filename*=UTF-8''" + quote(name))
        if status == 206:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
        self.end_headers()

        try:
            with open(path, "rb") as f:
                f.seek(start)
                remaining = length
                buf = bytearray(READ_BLOCK)
                view = memoryview(buf)
                while remaining > 0:
                    n = f.readinto(view[:min(READ_BLOCK, remaining)])
                    if not n:
                        break
                    self.wfile.write(view[:n])
                    remaining -= n
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    # ------------------------- POST -------------------------
    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        try:
            if path == "/api/trace":
                # 客户端埋点：把浏览器端关键事件写入日志，便于排查 iPad 端卡住
                self._discard_body()
                self._log_trace(qs.get("msg", [""])[0][:300])
                self._json(200, {"ok": True})
            elif path == "/upload":
                # 兼容最早版本页面的单次上传接口
                self._api_legacy_upload(qs)
            elif path == "/delete":
                # 兼容最早版本页面的删除接口
                self._api_delete(qs)
            elif path == "/api/init":
                self._api_init(qs)
            elif path == "/api/part":
                self._api_part(qs)
            elif path == "/api/finish":
                self._api_finish(qs)
            elif path == "/api/cancel":
                self._api_cancel(qs)
            elif path == "/api/delete":
                self._api_delete(qs)
            else:
                self._discard_body()
                self._json(404, {"ok": False, "msg": "not found", "version": VERSION,
                                 "hint": "接口不存在，页面版本可能过旧，请刷新页面"})
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    def _api_legacy_upload(self, qs):
        """兼容最早版本页面的单次上传接口 POST /upload?name=xxx"""
        name = sanitize_filename(qs.get("name", [""])[0])
        if not name:
            self._discard_body()
            self._json(400, {"ok": False, "msg": "缺少文件名"})
            return

        length = int(self.headers.get("Content-Length") or 0)
        os.makedirs(SAVE_DIR, exist_ok=True)
        dest = unique_path(SAVE_DIR, name)
        try:
            with open(dest, "wb") as f:
                written = self._recv_to_file(f, 0, length)
        except Exception as e:
            self.close_connection = True
            self._json(500, {"ok": False, "msg": "写入失败: %s" % e})
            return

        if written < length:
            self.close_connection = True
            try:
                os.remove(dest)
            except OSError:
                pass
            self._json(400, {"ok": False, "msg": "上传中断，文件不完整"})
            return

        self._json(200, {"ok": True, "name": os.path.basename(dest), "size": written})

    def _prepare_session(self, name, size, mtime, req_chunk=0):
        """创建或复用上传会话（HTTP 与 WebSocket 共用同一套续传状态）"""
        uid = make_upload_id(name, size, mtime)
        part, meta, bmpath = self._paths(uid)

        os.makedirs(TMP_DIR, exist_ok=True)

        # 1) 读取既有记录，判断是不是同一个文件
        info = {}
        prev_chunk = 0
        saved_rel = ""
        if os.path.isfile(meta):
            try:
                with open(meta, "r", encoding="utf-8") as f:
                    info = json.load(f)
                if info.get("name") != name or int(info.get("size", 0)) != size:
                    info = {}
                else:
                    prev_chunk = int(info.get("chunk", 0) or 0)
                    saved_rel = info.get("saved") or ""
            except Exception:
                info = {}
                prev_chunk = 0
                saved_rel = ""

        # 客户端可申请更适合自己的分片大小(iPad 用小分片更稳),
        # 但只对"全新会话"生效 —— 已有进度的一律沿用原分片, 避免续传进度作废
        if prev_chunk > 0:
            chunk = prev_chunk
        elif 262144 <= req_chunk <= 33554432 and (req_chunk & (req_chunk - 1)) == 0:
            chunk = req_chunk
        else:
            chunk = CHUNK_SIZE

        total = chunk_count(size, chunk)

        # 2) 该文件此前已传完、且电脑上文件仍在 -> 无需重传
        if info.get("done") and saved_rel:
            saved_abs = os.path.join(SAVE_DIR, saved_rel)
            try:
                if os.path.isfile(saved_abs) and os.path.getsize(saved_abs) == size:
                    return {"uid": uid, "part": part, "meta": meta, "bmp": bmpath,
                            "chunk": chunk, "total": total, "received": None,
                            "size": size, "done": True, "savedName": saved_rel}
            except OSError:
                pass
            info = {}

        # 3) 准备分片文件：预分配空间 + 位图（断点信息）
        need_new = True
        if os.path.isfile(part) and os.path.isfile(bmpath):
            try:
                if os.path.getsize(part) == size and os.path.getsize(bmpath) == total:
                    need_new = False
            except OSError:
                need_new = True

        if need_new:
            with open(part, "wb") as f:
                if size:
                    f.truncate(size)          # 预分配，避免边传边扩容
            with open(bmpath, "wb") as f:
                f.write(b"\x00" * total)
            with open(meta, "w", encoding="utf-8") as f:
                json.dump({"name": name, "size": size, "mtime": mtime,
                           "chunk": chunk,
                           "created": datetime.datetime.now().isoformat()}, f)
            received = []
        else:
            received = self._read_bitmap(bmpath, total)

        return {"uid": uid, "part": part, "meta": meta, "bmp": bmpath,
                "chunk": chunk, "total": total, "received": received,
                "size": size, "done": bool(size and len(received) >= total),
                "savedName": ""}

    def _finish_session(self, uid):
        """校验所有分片齐全后移入「接收的文件」（HTTP 与 WebSocket 共用）"""
        part, meta, bmpath = self._paths(uid)
        if not part or not os.path.isfile(part):
            return {"ok": False, "msg": "没有可完成的上传"}

        name = ""
        size = 0
        chunk = CHUNK_SIZE
        try:
            with open(meta, "r", encoding="utf-8") as f:
                info = json.load(f)
            name = info.get("name", "")
            size = int(info.get("size", 0))
            chunk = int(info.get("chunk", CHUNK_SIZE) or CHUNK_SIZE)
        except Exception:
            pass

        total = chunk_count(size, chunk)
        received = self._read_bitmap(bmpath, total)
        if total and len(received) < total:
            return {"ok": False, "msg": "还有 %d 个分片未完成" % (total - len(received)),
                    "received": len(received), "total": total}

        try:
            got = os.path.getsize(part)
        except OSError:
            got = 0
        if size and got != size:
            return {"ok": False, "msg": "文件大小异常 (%d/%d)" % (got, size)}

        name = sanitize_filename(name) or ("上传文件_" +
                                          datetime.datetime.now().strftime("%Y%m%d_%H%M%S"))
        os.makedirs(SAVE_DIR, exist_ok=True)
        dest = unique_path(SAVE_DIR, name)
        try:
            shutil.move(part, dest)
        except Exception as e:
            return {"ok": False, "msg": "保存失败: %s" % e}

        try:
            if os.path.isfile(bmpath):
                os.remove(bmpath)
        except OSError:
            pass

        # 保留一条完成记录：客户端在收到响应前断线并重试时可直接告知已完成，
        # 避免 20G 文件从头重传
        try:
            with open(meta, "w", encoding="utf-8") as f:
                json.dump({"name": name, "size": got, "done": True,
                           "chunk": chunk,
                           "saved": os.path.basename(dest),
                           "finished": datetime.datetime.now().isoformat()}, f)
        except OSError:
            pass

        return {"ok": True, "name": os.path.basename(dest),
                "size": os.path.getsize(dest)}

    def _api_init(self, qs):
        """初始化上传：返回 uploadId、分片大小、总分片数与已完成分片（断点续传核心）"""
        name = sanitize_filename(qs.get("name", [""])[0])
        if not name:
            self._json(400, {"ok": False, "msg": "缺少文件名"})
            return

        try:
            size = int(qs.get("size", ["0"])[0])
        except ValueError:
            size = 0
        if size < 0:
            size = 0
        mtime = qs.get("mtime", ["0"])[0]
        try:
            req_chunk = int(qs.get("chunk", ["0"])[0])
        except ValueError:
            req_chunk = 0

        s = self._prepare_session(name, size, mtime, req_chunk)
        if s["done"]:
            self._json(200, {"ok": True, "uploadId": s["uid"], "chunk": s["chunk"],
                             "total": s["total"], "received": list(range(s["total"])),
                             "size": size, "done": True,
                             "savedName": s.get("savedName", "")})
            return

        self._json(200, {"ok": True, "uploadId": s["uid"], "chunk": s["chunk"],
                         "total": s["total"], "received": s["received"],
                         "size": size, "done": False})

    def _api_part(self, qs):
        """接收一个分片：按 index 随机写入（支持多分片并发乱序到达）"""
        uid = qs.get("uploadId", [""])[0]
        part, meta, bmpath = self._paths(uid)
        if not part:
            self._discard_body()
            self._json(400, {"ok": False, "msg": "无效的 uploadId"})
            return

        try:
            index = int(qs.get("index", ["-1"])[0])
        except ValueError:
            index = -1

        # 兼容旧版页面：允许只传 offset（offset = index * 分片大小）
        if index < 0 and qs.get("offset"):
            try:
                index = int(qs.get("offset", ["0"])[0]) // CHUNK_SIZE
            except ValueError:
                index = -1

        if not os.path.isfile(part) or not os.path.isfile(bmpath):
            self._discard_body()
            self._json(409, {"ok": False, "error": "no_session",
                             "msg": "会话已失效", "offset": 0})
            return

        try:
            with open(meta, "r", encoding="utf-8") as f:
                info = json.load(f)
            size = int(info.get("size", 0))
            chunk = int(info.get("chunk", CHUNK_SIZE))
            name = info.get("name", "")
        except Exception:
            self._discard_body()
            self._json(409, {"ok": False, "error": "no_session", "msg": "会话信息缺失"})
            return

        total = chunk_count(size, chunk)      # 必须用会话自己的分片大小算
        if index < 0 or index >= total:
            self._discard_body()
            self._json(400, {"ok": False, "error": "bad_index",
                             "msg": "分片序号越界", "index": index, "total": total})
            return

        # 已经收过该分片，直接返回成功（幂等，重试安全）
        try:
            with open(bmpath, "rb") as f:
                f.seek(index)
                if f.read(1) == b"\x01":
                    self._discard_body()
                    self._json(200, {"ok": True, "dup": True, "index": index,
                                     "received": index + 1, "total": total})
                    return
        except Exception:
            pass

        offset = index * chunk
        expect = min(chunk, size - offset)

        # 某些浏览器/中间设备会用 chunked 编码上传（没有 Content-Length）
        chunked = "chunked" in (self.headers.get("Transfer-Encoding") or "").lower()
        length = 0
        if not chunked:
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            if length <= 0:
                self._discard_body()
                self._json(400, {"ok": False, "msg": "空分片"})
                return

        try:
            with open(part, "r+b") as f:
                if chunked:
                    written = self._recv_chunked_to_file(f, offset)
                else:
                    written = self._recv_to_file(f, offset, length)
        except Exception as e:
            self.close_connection = True
            self._json(500, {"ok": False, "msg": "写入失败: %s" % e})
            return

        if written < length:
            # 连接中断：该分片下次重传，不影响其它分片
            self.close_connection = True
            self._json(200, {"ok": False, "error": "short",
                             "msg": "传输中断", "index": index})
            return

        if written < expect:
            # 分片内容不足（正常不应发生）
            self._json(400, {"ok": False, "error": "short_chunk",
                             "msg": "分片不完整 (%d/%d)" % (written, expect),
                             "index": index})
            return

        try:
            self._mark_bitmap(bmpath, index)
        except Exception as e:
            self.close_connection = True
            self._json(500, {"ok": False, "msg": "位图写入失败: %s" % e})
            return

        self._json(200, {"ok": True, "index": index, "received": index + 1,
                         "total": total, "name": name})

    def _api_finish(self, qs):
        """校验所有分片齐全后，移动到「接收的文件」"""
        uid = qs.get("uploadId", [""])[0]
        r = self._finish_session(uid)
        # 用 400 而非 404：这是业务状态，避免前端误判成"接口不存在"
        self._json(200 if r.get("ok") else 400, r)

    def _api_cancel(self, qs):
        uid = qs.get("uploadId", [""])[0]
        part, meta, bmpath = self._paths(uid)
        if part:
            for p in (part, meta, bmpath):
                try:
                    if os.path.isfile(p):
                        os.remove(p)
                except OSError:
                    pass
        self._json(200, {"ok": True})

    def _api_delete(self, qs):
        path = self._safe_saved_file(qs.get("name", [""])[0])
        if path and os.path.isfile(path):
            try:
                os.remove(path)
                self._json(200, {"ok": True})
            except Exception as e:
                self._json(500, {"ok": False, "msg": str(e)})
        else:
            self._json(404, {"ok": False, "msg": "文件不存在"})

    # ------------------------- 日志 -------------------------
    def log_message(self, fmt, *args):
        """所有请求都写入 server.log（含状态码），便于排查问题"""
        msg = fmt % args
        if "/api/probe" in msg:
            return
        stamp = datetime.datetime.now().strftime("%m-%d %H:%M:%S")
        line = "[%s] %s  %s" % (stamp, self.client_address[0], msg)
        try:
            with LOG_LOCK:
                with open(LOG_FILE, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
        except Exception:
            pass
        # 分片请求太频繁，不打印到控制台，避免刷屏
        if "/api/part" in msg or "/api/init" in msg:
            return
        print("  " + line)


def clean_stale_tmp():
    """只清理超过 TMP_KEEP_DAYS 天的临时文件。
    保留近期的分片，这样服务器重启后大文件仍可从断点继续，不必重传。"""
    cutoff = time.time() - TMP_KEEP_DAYS * 86400
    n = 0
    try:
        for f in os.listdir(TMP_DIR):
            p = os.path.join(TMP_DIR, f)
            try:
                if os.path.isfile(p) and os.path.getmtime(p) < cutoff:
                    os.remove(p)
                    n += 1
            except OSError:
                pass
    except Exception:
        pass
    return n


def main():
    os.makedirs(SAVE_DIR, exist_ok=True)
    os.makedirs(TMP_DIR, exist_ok=True)

    # 端口占用检测：避免重复启动多个服务器，导致请求被随机分发到旧实例
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.settimeout(1.0)
        occupied = probe.connect_ex(("127.0.0.1", PORT)) == 0
    except Exception:
        occupied = False
    finally:
        try:
            probe.close()
        except Exception:
            pass
    if occupied:
        print("=" * 60)
        print("  端口 %d 已被占用，很可能已经有一个服务器在运行。" % PORT)
        print("  请先关闭原来那个命令行窗口，再重新启动本程序。")
        print("  （同时运行多个服务器会让请求被随机分发，出现各种异常）")
        print("=" * 60)
        try:
            input("  按回车键退出...")
        except Exception:
            pass
        return

    cleaned = clean_stale_tmp()
    if cleaned:
        print("  已清理 %d 个超过 %d 天的临时文件" % (cleaned, TMP_KEEP_DAYS))

    server = TransferServer(("0.0.0.0", PORT), FileHandler)

    print("=" * 60)
    print("  内网文件互传服务器已启动")
    print("  保存位置: %s" % SAVE_DIR)
    print("-" * 60)
    ips = get_lan_ips()
    if ips:
        print("  iPad / 手机 / 其他电脑 请用浏览器访问:")
        for ip in ips:
            print("      http://%s:%d" % (ip, PORT))
    else:
        print("  未能获取局域网 IP，请用 ipconfig 查看本机 IPv4 地址")
    print("      http://127.0.0.1:%d   (仅本机测试)" % PORT)
    print("-" * 60)
    print("  · 单文件支持 20G 以上，多分片并发 + 断网自动续传")
    print("  · 电脑也可以上传文件，iPad 打开同一网址即可下载")
    print("  · 分片 %d MB，页面可调并发数(默认 %d)" % (CHUNK_SIZE // (1024*1024), DEFAULT_PARALLEL))
    print("  · 首次运行如弹出防火墙提示，请勾选「专用网络」并允许访问")
    print("  · 关闭此窗口即停止服务")
    print("=" * 60)

    if AUTO_OPEN_BROWSER:
        try:
            webbrowser.open("http://127.0.0.1:%d" % PORT)
        except Exception:
            pass

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n服务器已停止。")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
