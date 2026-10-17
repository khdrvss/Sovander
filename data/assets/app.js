"use strict";

/*
 * Shared helpers for epub.html and md.html, from Gander's viewer/app.js (MIT, see LICENSES.md).
 *
 * Sovander keeps only what those two pages use. The Android parts are gone: the card for a
 * WebView too old to update, the dp arithmetic for a title bar floating over the page, the
 * text pager, the JSON layout and the print sizes. What replaces Gander's MessagePort is the
 * section at the foot of this file.
 */

var vwParams = new URLSearchParams(location.search);
var vwExt = (vwParams.get("ext") || "").toLowerCase();

/*
 * Sovander: the pages' words in the app's language (Requirement 14). web_view.py sets
 * window.vwWords, English to the words to show, before any script here runs, and sends "W" and
 * new words when the language changes. A word it does not give stays English; {name} in a word
 * is filled from [values].
 */
function vwSay(text, values) {
  var own = Object.prototype.hasOwnProperty;
  var words = window.vwWords;
  var said = words && own.call(words, text) ? words[text] : null;
  if (typeof said !== "string" || !said) said = String(text);
  return said.replace(/\{(\w+)\}/g, function (all, name) {
    return values && own.call(values, name) ? String(values[name]) : all;
  });
}

/* Every element with a data-vw-word, its English, says it in the app's language */
function vwRelabel() {
  var marked = document.querySelectorAll("[data-vw-word]");
  for (var i = 0; i < marked.length; i++) marked[i].textContent = vwSay(marked[i].getAttribute("data-vw-word"));
}

vwRelabel();

/* An Error whose message is one of the app's words, its {names} from [values] */
function vwProblem(detail, values) {
  var e = new Error(detail);
  e.vwValues = values || null;
  return e;
}

/* The error card on screen, to say again in another language */
var vwShownError = null;

/* Nothing floats over a Sovander page, so a chapter starts at the top of the screen */
var vwTopPx = 0;
var vwBarGonePx = 0;

function vwStatusDone() {
  var el = document.getElementById("vw-status");
  if (el) el.style.display = "none";
  vwPost("ready");
}

function vwError(title, detail, values) {
  vwShownError = [title, detail, values];
  var el = document.getElementById("vw-status");
  if (!el) {
    el = document.createElement("div");
    el.id = "vw-status";
    document.body.insertBefore(el, document.body.firstChild);
  }
  el.removeAttribute("data-vw-word");
  el.className = "vw-status vw-error";
  el.style.display = "";
  el.innerHTML = "";
  var t = document.createElement("div");
  t.className = "vw-error-title";
  t.textContent = vwSay(title, values);
  var d = document.createElement("div");
  d.className = "vw-error-detail";
  d.textContent = detail ? vwSay(detail, values) : "";
  el.appendChild(t);
  el.appendChild(d);
  vwPost("error " + t.textContent + "\n" + d.textContent);
}

/* Why the document could not be shown: [e]'s own title and words where it has them */
function vwFail(title, e) {
  vwError((e && e.vwTitle) || title, String((e && e.message) || e), e && e.vwValues);
}

function vwDocUrl() {
  return "/doc/file" + (vwExt ? "." + vwExt : "");
}

/*
 * How large a document has to be before the card goes up for reading it. .vw-wait holds the
 * card back for 700 ms, and only the page's main thread can end that; a large document read
 * in one piece on that thread would leave the reader on a blank page instead.
 */
var VW_BUSY_BYTES = 1024 * 1024;

/* [doc], once the card is on screen if [size] says reading it will take a while. */
function vwShowBusy(doc, size) {
  var el = document.getElementById("vw-status");
  if (size < VW_BUSY_BYTES || !el || el.style.display === "none") return doc;
  el.classList.remove("vw-wait");
  return new Promise(function (resolve) {
    // The frame the card is drawn in comes after this callback, and the timeout after it
    requestAnimationFrame(function () { setTimeout(function () { resolve(doc); }, 0); });
  });
}

/* Fetch the document being viewed. kind: "buffer" | "text" */
function vwFetchDoc(kind) {
  return fetch(vwDocUrl()).then(function (r) {
    if (!r.ok) throw vwProblem("Could not read the file (HTTP {status})", { status: r.status });
    return kind === "text" ? r.text() : r.arrayBuffer();
  }).then(function (doc) {
    return vwShowBusy(doc, kind === "text" ? doc.length : doc.byteLength);
  });
}

window.onerror = function (message) {
  vwError("Something went wrong while rendering", String(message));
};

// -----------------------------------------------------------------------------------------
// Sovander: the channel to the app, and night
// -----------------------------------------------------------------------------------------

/*
 * Says [msg] to the app through WebKit's script message handler "sovander", which
 * sovander/views/web_view.py registers. Answers whether there was an app to hear it, as
 * Gander's port did. Every message is text; the app reads them in WebView._on_message and
 * ignores anything it does not know.
 */
function vwPost(msg) {
  var handlers = window.webkit && window.webkit.messageHandlers;
  if (!handlers || !handlers.sovander) return false;
  handlers.sovander.postMessage(String(msg));
  return true;
}

/*
 * Night is the app's to say, not the system's: night=1 on the URL, then "N1" or "N0" as a
 * command. Gander followed prefers-color-scheme, which WebKitGTK cannot set per view, so
 * every rule Gander wrote under that query is under :root.vw-night here instead.
 */
var vwNightOn = vwParams.get("night") === "1";

function vwNight() {
  return vwNightOn;
}

function vwSetNight(on) {
  vwNightOn = !!on;
  document.documentElement.classList.toggle("vw-night", vwNightOn);
  if (window.vwOnNight) window.vwOnNight(vwNightOn);
}

document.documentElement.classList.toggle("vw-night", vwNightOn);

/* The app's commands that are not a search's: see find.js. Answers whether it was one. */
function vwAppSaid(msg) {
  if (msg === "N1" || msg === "N0") {
    vwSetNight(msg === "N1");
    return true;
  }
  if (msg.charAt(0) === "W") {
    var words = null;
    try { words = JSON.parse(msg.slice(1)); } catch (e) { /* the words stay as they were */ }
    if (words && typeof words === "object") {
      window.vwWords = words;
      vwRelabel();
      if (vwShownError) vwError(vwShownError[0], vwShownError[1], vwShownError[2]);
      if (window.vwOnWords) window.vwOnWords();
    }
    return true;
  }
  return false;
}
