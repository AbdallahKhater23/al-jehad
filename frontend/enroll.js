
(function () {
  "use strict";
  var API = "/api/v1";
  var token = decodeURIComponent(location.pathname.split("/").filter(Boolean).pop() || "");
  var stream = null, blob = null;

  // The server's answer replaces this, so the page never has to be told twice what the
  // limit is. The defaults are the same numbers, so a link whose response has not
  // arrived yet still refuses a 40 MB video rather than uploading it.
  var policy = { max_bytes: 5 * 1024 * 1024, accepted: ["image/jpeg", "image/png", "image/webp"] };
  var minPasswordLength = 8;
  var isRegister = false;

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
   * The real check is the server's, from the bytes, because the file name and the
   * browser's idea of the type are both supplied by whoever sends the request - this
   * only saves somebody a 6 MB upload over a phone tether.
   */
  function checkPhoto(file) {
    if (!file) return "Take or choose a photo first.";
    var size = Number(file.size || 0);
    if (size <= 0) return "That file is empty. Choose a photo from the gallery.";
    if (size > Number(policy.max_bytes || 0)) {
      return "That photo is " + mb(size) + " MB and the limit is " +
             mb(policy.max_bytes) + " MB. Retake it at a lower resolution.";
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

  function loadInvite() {
    fetch(API + "/enroll/" + encodeURIComponent(token))
      .then(function (r) { return r.json().then(function (b) { return { ok: r.ok, body: b }; }); })
      .then(function (res) {
        if (!res.ok) {
          say((res.body && (res.body.detail && res.body.detail.message || res.body.detail)) || "This link is not valid.", "err");
          $("btn-start").disabled = true; $("btn-submit").disabled = true; $("fallback-label").classList.add("hidden");
          return;
        }
        if (res.body.photo_policy) policy = res.body.photo_policy;
        if (res.body.min_password_length) minPasswordLength = Number(res.body.min_password_length);
        applyPolicy();

        isRegister = res.body.kind === "register";
        var verb = isRegister ? "Creating an account for" : "Enrolling";
        $("who").innerHTML = verb + " <strong>" + res.body.worker_name + "</strong> (id " +
          res.body.worker_id + ") · <span class='pill'>expires " + res.body.expires_at + "</span>";

        if (isRegister) {
          // The password is the visitor's own choice and nobody can reset it for them,
          // so the rule is stated before they type rather than after they submit.
          $("title").textContent = "Create your account";
          $("password-card").classList.remove("hidden");
          $("password-hint").textContent =
            "Choose a password of at least " + minPasswordLength + " characters. You will sign in " +
            "with your ID (" + res.body.worker_id + ") and this password, so keep it safe.";
          $("btn-submit").textContent = "Create my account";
        }

        if (!res.body.usable) {
          say("This link is " + res.body.status + ". Ask your administrator for a new one.", "err");
          $("btn-start").disabled = true;
          $("btn-submit").disabled = true;
          $("password-card").classList.add("hidden");
        }
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
        say("Camera blocked. Use the button below to open your phone's gallery.", "err");
      });
  }

  function choose(file) {
    var problem = checkPhoto(file);
    showPhotoProblem(problem);
    if (problem) {
      blob = null;
      $("btn-submit").disabled = true;
      return;
    }
    blob = file;
    $("preview").src = URL.createObjectURL(file);
    $("preview").classList.remove("hidden");
    $("video").classList.add("hidden");
    $("btn-submit").disabled = false;
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
    blob = null; $("btn-submit").disabled = true;
    showPhotoProblem("");
    $("btn-shoot").classList.remove("hidden");
    $("btn-retake").classList.add("hidden");
    startCamera();
  }

  function submit() {
    var problem = checkPhoto(blob);
    if (problem) {
      showPhotoProblem(problem);
      say(problem, "err");
      return;
    }
    var form = new FormData();
    form.append("photo", blob, "photo.jpg");
    form.append("phone", $("phone").value || "");
    form.append("email", $("email").value || "");
    var url = API + "/enroll/" + encodeURIComponent(token);

    if (isRegister) {
      var password = $("password").value || "";
      var again = $("password2").value || "";
      if (password.length < minPasswordLength) {
        say("Your password needs at least " + minPasswordLength + " characters.", "err");
        return;
      }
      if (password !== again) {
        say("The two passwords are not the same.", "err");
        return;
      }
      form.append("password", password);
      url += "/register";
    }

    $("btn-submit").disabled = true;
    $("status-line").textContent = "Uploading…";
    fetch(url, { method: "POST", body: form })
      .then(function (r) { return r.json().then(function (b) { return { ok: r.ok, body: b }; }); })
      .then(function (res) {
        if (res.ok) {
          if (isRegister) {
            say("Done. Your account is ready - sign in with your ID and the password you chose.", "ok");
            $("password-card").classList.add("hidden");
          } else {
            say("Done. Your reference photo has been registered.", "ok");
          }
          var live = res.body.liveness || {};
          $("status-line").textContent = "Liveness: " + (live.verdict || "n/a");
          $("btn-retake").classList.add("hidden");
        } else {
          var detail = res.body && res.body.detail;
          var code = detail && detail.error_code ? " (" + detail.error_code + ")" : "";
          var text = (detail && detail.message) || detail || "Enrollment failed.";
          say(text + code, "err");
          $("btn-submit").disabled = false;
          $("status-line").textContent = "";
        }
      })
      .catch(function () { say("Upload failed. Check your connection and try again.", "err"); $("btn-submit").disabled = false; });
  }

  $("btn-start").addEventListener("click", startCamera);
  $("btn-shoot").addEventListener("click", shoot);
  $("btn-retake").addEventListener("click", retake);
  $("btn-submit").addEventListener("click", submit);
  $("file").addEventListener("change", function (event) {
    var file = event.target.files && event.target.files[0];
    if (file) choose(file);
  });

  applyPolicy();
  loadInvite();
})();
