/* Sleevenote — reading a barcode with the phone's own camera.
 *
 * The rule this file lives under: JavaScript may improve a page; it may never
 * carry a fact. Nothing here writes anything and nothing here decides anything.
 * There is no fetch, no XMLHttpRequest, no sendBeacon and no form submission —
 * tests.py:TestPromises.test_no_javascript_on_the_shelf_can_write_anything greps
 * this file for all four, and for localStorage and indexedDB besides.
 *
 * All it does is put thirteen digits into the text box that was already on the
 * page, mark the hidden "how" field as a camera read (so the scan log can tell a
 * misread from a mistype), and stop. Edit the digits by hand afterwards and the
 * field goes back to "typed", because then they are yours. You still press the button, the barcode still travels as an
 * ordinary form post, and the server still checks the digits against their own
 * check digit before looking anything up. Switch JavaScript off and the box, the
 * button and the photo upload all work exactly as they did.
 *
 * It deliberately does NOT submit the form for you. A camera can misread, and a
 * misread barcode that files itself is a wrong record in the collection with
 * nothing to show it went wrong. Seeing the digits before you press is the whole
 * safeguard, and it costs one tap.
 *
 * BarcodeDetector is native to Chrome on Android; Safari on the iPhone does not
 * have it. Where it does not exist, or the page came over plain http (where the
 * browser will not hand over the camera at all), the button never appears and the
 * page says to photograph the barcode instead — which is read by the server, in
 * Python, and needs nothing installed on the phone at all.
 *
 * The button is `hidden` in the HTML and only this file un-hides it, so with
 * scripting off there is no button that does nothing. The stylesheet's
 * `[hidden]{display:none!important}` is what makes that true: `.btn` sets a
 * display of its own, and without that rule it beat the attribute and showed a
 * dead button on every browser that cannot scan.
 */
(function () {
  "use strict";

  var btn = document.getElementById("scanbtn");
  var msg = document.getElementById("scanmsg");
  var vid = document.getElementById("scanvid");
  var box = document.getElementById("code");
  var how = document.getElementById("how");
  if (!btn || !msg || !vid || !box) { return; }

  var WANT = ["ean_13", "upc_a", "ean_8", "upc_e"];
  var NO_READER = "This browser cannot read a barcode from the camera itself. Use "
    + "\u201cOr photograph it\u201d below \u2014 on a phone it opens the camera, and the "
    + "photo is read here, by the server. Or type the digits in.";

  /* The order matters. Over plain http the browser hides navigator.mediaDevices
     altogether, so checking for a camera first would blame the browser for what
     is really the address. */
  if (!window.isSecureContext) {
    msg.textContent = "The camera only works on an https address, and this page came over "
      + "plain http. Photographing the barcode or typing the digits works here; for the "
      + "camera, open this app at its https address (the README says how).";
    return;
  }
  if (!("BarcodeDetector" in window)) { msg.textContent = NO_READER; return; }
  if (!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia)) {
    msg.textContent = "This browser will not hand the camera to a page. Photographing the "
      + "barcode or typing the digits works instead.";
    return;
  }

  /* Some desktop browsers have BarcodeDetector but no barcode formats behind it,
     and a camera that can never find anything is worse than no button. */
  var formats = WANT;
  function offer() {
    msg.textContent = "This browser can read a barcode with the camera.";
    btn.hidden = false;
  }
  try {
    window.BarcodeDetector.getSupportedFormats().then(function (have) {
      var ok = WANT.filter(function (f) { return have.indexOf(f) !== -1; });
      if (ok.length) { formats = ok; offer(); } else { msg.textContent = NO_READER; }
    }, offer);
  } catch (err) {
    offer();
  }

  var stream = null, timer = null, detector = null;

  function stop(text) {
    if (timer) { clearInterval(timer); timer = null; }
    if (stream) {
      stream.getTracks().forEach(function (t) { t.stop(); });
      stream = null;
    }
    vid.hidden = true;
    btn.disabled = false;
    btn.textContent = "Start the camera";
    if (text) { msg.textContent = text; }
  }

  /* Keep digits only. The server normalises and checksums it again regardless;
     this is so the box holds something a person recognises. */
  function digits(raw) { return String(raw || "").replace(/\D/g, ""); }

  function found(raw) {
    var d = digits(raw);
    if (d.length !== 8 && d.length !== 12 && d.length !== 13) { return; }
    box.value = d;
    if (how) { how.value = "camera"; }
    stop("Read " + d + " — check it against the sleeve, then press Look it up.");
    try { box.focus(); box.setSelectionRange(d.length, d.length); } catch (err) { /* no-op */ }
  }

  btn.addEventListener("click", function (ev) {
    ev.preventDefault();
    if (stream) { stop("Stopped."); return; }
    btn.disabled = true;
    msg.textContent = "Starting the camera…";
    try {
      detector = detector || new window.BarcodeDetector({ formats: formats });
    } catch (err) {
      stop("This browser cannot read this kind of barcode.");
      return;
    }
    navigator.mediaDevices.getUserMedia({
      video: { facingMode: { ideal: "environment" } }
    }).then(function (s) {
      stream = s;
      vid.srcObject = s;
      vid.hidden = false;
      btn.disabled = false;
      btn.textContent = "Stop the camera";
      msg.textContent = "Point it at the barcode on the back of the sleeve.";
      return vid.play();
    }).then(function () {
      timer = setInterval(function () {
        if (!stream) { return; }
        detector.detect(vid).then(function (codes) {
          if (codes && codes.length && codes[0].rawValue) { found(codes[0].rawValue); }
        }).catch(function () { /* a frame that would not decode; try the next */ });
      }, 350);
    }).catch(function () {
      stop("No camera, or permission was refused. Photographing the barcode works too.");
    });
  });

  /* A value set from script fires no input event, so this only runs when a
     person types: the digits are then theirs, not the camera's. */
  box.addEventListener("input", function () { if (how) { how.value = "typed"; } });

  window.addEventListener("pagehide", function () { stop(""); });
})();
