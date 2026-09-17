
(function () {
  "use strict";
  var API = "/api/v1";
  var token = decodeURIComponent(location.pathname.split("/").filter(Boolean).pop() || "");
  var stream = null, blob = null, fix = null, info = null;

  // The server's answer replaces this, so the page never has to be told twice. The defaults
  // are the same numbers, so a link whose response has not arrived yet still refuses a 40 MB
  // video rather than uploading it.
  var policy = { max_bytes: 5 * 1024 * 1024, accepted: ["image/jpeg", "image/png", "image/webp"] };

  var $ = function (id) { return document.getElementById(id); };

  function say(text, kind) {
    var box = $("message");
    box.className = "msg " + (kind || "");
    box.textContent = text;
  }

  function mb(bytes) {
    return (Number(bytes || 0) / (1024 * 1024)).toFixed(1);
  }

  /**
   * Why this file cannot be used, or null when it can.
   *
   * The real check is the server's, from the bytes: the file name and the browser's idea of
   * the type are both supplied by whoever sends the request. This only saves somebody an
   * upload over a phone tether.
   */
  function checkPhoto(file) {
    if (!file) return "Take or choose a selfie first.";
    var size = Number(file.size || 0);
    if (size <= 0) return "That file is empty. Take the selfie again.";
    if (size > Number(policy.max_bytes || 0)) {
      return "That photo is " + mb(size) + " MB and the limit is " + mb(policy.max_bytes) +
             " MB. Retake it at a lower resolution.";
    }
    var type = String(file.type || "").toLowerCase();
    if (policy.accepted.indexOf(type) < 0) {
      var name = String(file.name || "").toLowerCase();
      if (!(type === "" && /\.(jpe?g|png|webp)$/.test(name))) {
        return "That file is not a photo. Only " + policy.accepted.join(", ") +
               " are accepted - no documents, PDFs or videos.";
      }
    }
    return null;
  }

  function showPhotoProblem(problem) {
    var box = $("photo-problem");
    box.className = problem ? "note warn" : "note warn hidden";
    box.textContent = problem || "";
  }

  function applyPolicy() {
    var list = policy.accepted.map(function (type) { return type.replace("image/", "").toUpperCase(); });
    $("photo-policy").textContent =
      "Photos only: " + list.join(", ") + ", up to " + mb(policy.max_bytes) + " MB.";
    $("file").setAttribute("accept", policy.accepted.join(","));
  }

  function describeState() {
    if (!info) return;
    if (info.clocked_in) {
      $("state").textContent = "You are clocked in";
      $("state-detail").textContent = "Since " + info.clock_in_time +
        (info.open_shift_site ? " at " + info.open_shift_site : "") + ".";
    } else {
      $("state").textContent = "You are clocked out";
      $("state-detail").textContent = "Your last shift is closed.";
    }
    $("btn-punch").textContent = info.next_action === "Clock Out" ? "Clock out" : "Clock in";
    if (info.remaining_uses !== null && info.remaining_uses !== undefined) {
      $("state-detail").textContent += " This link has " + info.remaining_uses + " tap(s) left.";
    }
  }

  function load() {
    fetch(API + "/q/" + encodeURIComponent(token))
      .then(function (r) { return r.json().then(function (b) { return { ok: r.ok, body: b }; }); })
      .then(function (res) {
        var detail = res.body && res.body.detail;
        if (!res.ok) {
          // A dead link (revoked, expired, used up, or an account that was deactivated) is a
          // final answer, not a retry: the page says which of those it is and disables the tap.
          var text = (detail && detail.message) || detail || "This link is not valid.";
          var code = detail && detail.error_code ? " (" + detail.error_code + ")" : "";
          say(text + code, "err");
          $("btn-punch").disabled = true;
          $("btn-start").disabled = true;
          $("fallback-label").classList.add("hidden");
          return;
        }
        info = res.body;
        if (info.photo_policy) policy = info.photo_policy;
        applyPolicy();
        $("who").innerHTML = "For <strong>" + info.worker_name + "</strong> (id " + info.worker_id +
          ") · <span class='pill'>expires " + info.expires_at + "</span>";
        describeState();
      })
      .catch(function () { say("Could not reach the server. Check your connection.", "err"); });
  }

  function startCamera() {
    navigator.mediaDevices.getUserMedia({ video: { facingMode: "user", width: { ideal: 720 } }, audio: false })
      .then(function (s) {
        stream = s;
        $("video").srcObject = s;
        $("video").classList.remove("hidden");
        $("preview").classList.add("hidden");
        $("btn-shoot").classList.remove("hidden");
        $("btn-retake").classList.add("hidden");
        $("btn-start").classList.add("hidden");
        $("fallback-label").classList.add("hidden");
      })
      .catch(function () {
        say("Camera blocked. Use the button below to open your phone's camera app instead.", "err");
      });
  }

  function choose(file) {
    var problem = checkPhoto(file);
    showPhotoProblem(problem);
    if (problem) {
      blob = null;
      $("btn-punch").disabled = true;
      return;
    }
    blob = file;
    $("preview").src = URL.createObjectURL(file);
    $("preview").classList.remove("hidden");
    $("video").classList.add("hidden");
    $("btn-punch").disabled = false;
    $("btn-punch").textContent = "Send this selfie";
  }

  function shoot() {
    var video = $("video"), canvas = $("canvas");
    canvas.width = video.videoWidth || 720;
    canvas.height = video.videoHeight || 960;
    canvas.getContext("2d").drawImage(video, 0, 0, canvas.width, canvas.height);
    canvas.toBlob(function (b) {
      choose(b);
      $("btn-shoot").classList.add("hidden");
      $("btn-retake").classList.remove("hidden");
      stopCamera();
    }, "image/jpeg", 0.92);
  }

  function stopCamera() {
    if (stream) { stream.getTracks().forEach(function (t) { t.stop(); }); stream = null; }
  }

  function retake() {
    blob = null; $("btn-punch").disabled = true;
    showPhotoProblem("");
    $("btn-shoot").classList.remove("hidden");
    $("btn-retake").classList.add("hidden");
    startCamera();
  }

  /**
   * The location fix, or an explanation of why there is none.
   *
   * The site's geofence still applies to a link - that is what keeps it from being a way to
   * clock in from home - so the coordinates go with the punch and a refusal names the reason.
   * A high-accuracy fix is requested because a phone's coarse network fix can be kilometres
   * out, which would fail a geofence the worker is standing inside.
   */
  function locate() {
    return new Promise(function (resolve) {
      if (!navigator.geolocation) {
        $("location-line").textContent = "This browser cannot report a location, so the punch cannot be recorded.";
        resolve(null);
        return;
      }
      navigator.geolocation.getCurrentPosition(
        function (position) {
          fix = {
            lat: position.coords.latitude,
            lon: position.coords.longitude,
            accuracy: position.coords.accuracy
          };
          $("location-line").textContent = "Location found (±" + Math.round(fix.accuracy || 0) + " m).";
          resolve(fix);
        },
        function (error) {
          $("location-line").textContent = error && error.code === 1
            ? "Location is blocked. Allow location for this page and try again."
            : "Your location could not be determined. Step outside and try again.";
          resolve(null);
        },
        { enableHighAccuracy: true, timeout: 15000, maximumAge: 30000 }
      );
    });
  }

  function punch() {
    var problem = checkPhoto(blob);
    if (problem) {
      showPhotoProblem(problem);
      say(problem, "err");
      return;
    }
    $("btn-punch").disabled = true;
    say("Recording your tap…", "ok");
    locate().then(function (position) {
      if (!position) {
        say("No location fix, so the punch was not sent. Allow location and try again.", "err");
        $("btn-punch").disabled = false;
        describeState();
        return;
      }
      var form = new FormData();
      form.append("selfie", blob, "selfie.jpg");
      form.append("lat", String(position.lat));
      form.append("lon", String(position.lon));
      if (position.accuracy !== null && position.accuracy !== undefined) {
        form.append("accuracy", String(position.accuracy));
      }
      fetch(API + "/q/" + encodeURIComponent(token), { method: "POST", body: form })
        .then(function (r) { return r.json().then(function (b) { return { ok: r.ok, body: b }; }); })
        .then(function (res) {
          if (!res.ok) {
            var detail = res.body && res.body.detail;
            var text = (detail && detail.message) || detail || "The tap was not recorded.";
            var code = detail && detail.error_code ? " (" + detail.error_code + ")" : "";
            say(text + code, "err");
            $("btn-punch").disabled = false;
            return;
          }
          var body = res.body;
          var hours = Number(body.hours || 0);
          say(
            body.action === "Clock Out"
              ? "Clocked out of " + body.site + ". " + hours.toFixed(2) + "h paid" +
                (body.break_hours ? " after a " + Math.round(body.break_hours * 60) + "-minute break" : "") + "."
              : "Clocked in at " + body.site + ".",
            "ok"
          );
          blob = null;
          $("preview").classList.add("hidden");
          load();
        })
        .catch(function () {
          say("The tap did not reach the server. Check your connection and try again.", "err");
          $("btn-punch").disabled = false;
        });
    });
  }

  $("btn-punch").addEventListener("click", punch);
  $("btn-start").addEventListener("click", startCamera);
  $("btn-shoot").addEventListener("click", shoot);
  $("btn-retake").addEventListener("click", retake);
  $("file").addEventListener("change", function (event) {
    var file = event.target.files && event.target.files[0];
    if (file) choose(file);
  });

  applyPolicy();
  load();
  locate();
})();
