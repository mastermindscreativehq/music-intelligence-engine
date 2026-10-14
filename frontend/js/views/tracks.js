/* My music view: the operator's release library.
 *
 * A release is recorded by its metadata — title, artist, listening link,
 * optional storyline and genre — and saved through the real backend
 * `/api/v1/tracks/releases` endpoint. No MP3 upload is required (the legacy
 * content-addressed upload path still exists server-side and existing
 * uploads stay fully usable here). All data comes verbatim from the real
 * submission endpoints through api.js — there are no mocks and no invented
 * metadata. Per-track paths are taken from each projection's links.self
 * value supplied by the backend; this module never constructs asset URLs.
 */

import { api, ApiError } from "../api.js";
import { el } from "../dom.js";

/* Mirrors submissions.service.TRACK_STATUSES on the backend. */
const TRACK_STATUSES = ["ready", "quarantined", "archived"];
const LIMIT_CHOICES = [25, 50, 100, 200];
const AUDIO_EXTENSIONS = [".mp3", ".wav", ".flac", ".aiff", ".aif", ".m4a",
  ".ogg"];

function errorBanner(error) {
  const detail = error instanceof ApiError
    ? `${error.code}: ${error.message}`
    : String(error);
  return el("div", { class: "banner-error", role: "alert" },
    "Request failed. ", el("strong", {}, detail));
}

function statusChip(status) {
  const known = TRACK_STATUSES.includes(status);
  return el("span",
    { class: `chip${known ? ` ${status}` : ""}` },
    String(status ?? "unknown"));
}

/* The release title is the stored title when present; a legacy upload with
 * no title falls back to its filename (extension stripped). Never invented. */
function releaseTitle(record) {
  const title = String(record.title || "").trim();
  if (title) return title;
  let name = String(record.original_filename || "").trim();
  if (!name) return "(untitled release)";
  const lower = name.toLowerCase();
  for (const ext of AUDIO_EXTENSIONS) {
    if (lower.endsWith(ext)) {
      name = name.slice(0, -ext.length);
      break;
    }
  }
  return name || "(untitled release)";
}

function releaseArtist(record) {
  return String(record.artist_name || "").trim();
}

/* The saved official listening URL is a link when present and an honest
 * "not saved" note when absent — this module never invents a link. */
function listenLink(url) {
  const value = String(url || "").trim();
  if (!value) {
    return el("span", { class: "dim" }, "No listening URL saved yet.");
  }
  return el("a", {
    class: "linkish",
    href: value,
    target: "_blank",
    rel: "noopener noreferrer",
  }, value);
}

function storylinePreview(record) {
  const story = String(record.song_story || "").trim();
  if (!story) return "No storyline saved.";
  return story.length > 160 ? `${story.slice(0, 157)}…` : story;
}

/* Edit the release's operator-owned identity metadata (title, artist,
 * genre). Saving posts to the real PATCH endpoint; the backend validates and
 * refuses a blank title/artist, so a bad edit surfaces as a typed error
 * rather than a silent correction. */
function releaseMetadataEditor(record, onSaved) {
  const titleInput = el("input", {
    type: "text",
    class: "release-title-input",
    placeholder: "Release title",
    autocomplete: "off",
    value: record.title || "",
  });
  const artistInput = el("input", {
    type: "text",
    class: "release-artist-input",
    placeholder: "Artist name",
    autocomplete: "off",
    value: record.artist_name || "",
  });
  const genreInput = el("input", {
    type: "text",
    class: "release-genre-input",
    placeholder: "Genre (optional)",
    autocomplete: "off",
    value: record.genre || "",
  });
  const save = el("button", { class: "primary", type: "button" },
    "Save release");
  const status = el("p", { class: "dim", role: "status" });

  save.addEventListener("click", async () => {
    if (save.disabled) return;
    save.disabled = true;
    save.textContent = "saving…";
    try {
      await api.updateTrackRelease(record.track_id, {
        title: titleInput.value.trim(),
        artist_name: artistInput.value.trim(),
        genre: genreInput.value.trim(),
      });
      status.replaceChildren("Saved. Outreach for this release uses these "
        + "details automatically.");
      if (typeof onSaved === "function") onSaved();
    } catch (error) {
      const detail = error instanceof ApiError
        ? `${error.code}: ${error.message}` : String(error);
      status.replaceChildren(el("span", { class: "banner-error" }, detail));
    } finally {
      save.disabled = false;
      save.textContent = "Save release";
    }
  });

  return el("details", { class: "track-release", open: true },
    el("summary", {}, "Release details"),
    el("p", { class: "dim" },
      "The title and artist identify this release everywhere in the app, "
      + "including outreach."),
    el("label", { class: "field" }, el("span", {}, "Title"), titleInput),
    el("label", { class: "field" }, el("span", {}, "Artist"), artistInput),
    el("label", { class: "field" }, el("span", {}, "Genre"), genreInput),
    save,
    status);
}

/* Edit the one operator-owned presentation field on a release. Saving posts
 * to the real PATCH endpoint; the backend validates and refuses anything
 * that is not an absolute http(s) URL, so a bad link surfaces as a typed
 * error rather than a silent correction. */
function listenUrlEditor(record) {
  const input = el("input", {
    type: "url",
    class: "listen-url-input",
    placeholder: "Official listening page URL",
    autocomplete: "off",
    spellcheck: "false",
    value: record.listen_url || "",
  });
  const save = el("button", { class: "primary", type: "button" }, "Save link");
  const status = el("p", { class: "dim", role: "status" });
  const current = el("div", { class: "listen-current" },
    el("span", { class: "dim" }, "Current: "), listenLink(record.listen_url));

  const showCurrent = (url) =>
    current.replaceChildren(el("span", { class: "dim" }, "Current: "),
      listenLink(url));

  save.addEventListener("click", async () => {
    if (save.disabled) return;
    save.disabled = true;
    save.textContent = "saving…";
    try {
      const saved = await api.updateTrackListenUrl(
        record.track_id, input.value.trim());
      const url = saved.listen_url || "";
      input.value = url;
      showCurrent(url);
      status.replaceChildren(url
        ? "Saved. Outreach for this release uses this link automatically."
        : "Cleared. Outreach will ask you to add a link before it includes one.");
    } catch (error) {
      const detail = error instanceof ApiError
        ? `${error.code}: ${error.message}` : String(error);
      status.replaceChildren(el("span", { class: "banner-error" }, detail));
    } finally {
      save.disabled = false;
      save.textContent = "Save link";
    }
  });

  return el("details", { class: "track-listen", open: true },
    el("summary", {}, "Official listening URL"),
    el("p", { class: "dim" },
      "Where this release can be heard. Outreach reuses it automatically and "
      + "never invents one."),
    current,
    el("label", { class: "field" }, el("span", {}, "Listening URL"), input),
    save,
    status);
}

/* The song story / creative brief is the operator's OWN account of the song.
 * It is saved per release and used as source material when writing outreach;
 * it is never inferred from the filename, the audio, or anything else. */
function songStoryEditor(record) {
  const input = el("textarea", {
    class: "song-story-input",
    rows: "6",
    placeholder: "Inspiration, story, themes, emotions, audience, what "
      + "makes it different…",
    autocomplete: "off",
    spellcheck: "true",
  });
  input.value = record.song_story || "";
  const save = el("button", { class: "primary", type: "button" },
    "Save story");
  const status = el("p", { class: "dim", role: "status" });

  save.addEventListener("click", async () => {
    if (save.disabled) return;
    save.disabled = true;
    save.textContent = "saving…";
    try {
      const saved = await api.updateTrackSongStory(
        record.track_id, input.value);
      const story = saved.song_story || "";
      input.value = story;
      status.replaceChildren(story
        ? "Saved. Outreach can draw on this story for this release."
        : "Cleared. This release has no song story saved.");
    } catch (error) {
      const detail = error instanceof ApiError
        ? `${error.code}: ${error.message}` : String(error);
      status.replaceChildren(el("span", { class: "banner-error" }, detail));
    } finally {
      save.disabled = false;
      save.textContent = "Save story";
    }
  });

  return el("details", { class: "track-story" },
    el("summary", {}, "Song story / creative brief"),
    el("p", { class: "dim" },
      "Your own account of this song - inspiration, meaning, themes, "
      + "audience. Used as source material when writing outreach; the "
      + "writer never invents a song's meaning."),
    el("label", { class: "field" }, el("span", {}, "Story"), input),
    save,
    status);
}

/* Add a release from metadata alone: title, artist, listening link and the
 * optional storyline/genre. Persists through the real create endpoint; the
 * backend validates and derives the opaque id. Existing uploaded assets are
 * untouched and remain listed below. */
function releaseForm(onCreated) {
  const titleInput = el("input", {
    type: "text", class: "release-title-input", placeholder: "Release title",
    autocomplete: "off", required: true,
  });
  const artistInput = el("input", {
    type: "text", class: "release-artist-input", placeholder: "Artist name",
    autocomplete: "off", required: true,
  });
  const urlInput = el("input", {
    type: "url", class: "release-url-input",
    placeholder: "Listening link (Bandcamp, Spotify, YouTube…)",
    autocomplete: "off", required: true,
  });
  const genreInput = el("input", {
    type: "text", class: "release-genre-input", placeholder: "Genre (optional)",
    autocomplete: "off",
  });
  const storyInput = el("textarea", {
    class: "release-story-input", rows: "4",
    placeholder: "Storyline / creative note (optional)",
    autocomplete: "off",
  });
  const submit = el("button", { class: "primary", type: "submit" },
    "Add release");
  const resultLine = el("div", { class: "release-result" });

  const form = el("form", {
    class: "release-form",
    onSubmit: async (event) => {
      event.preventDefault();
      resultLine.replaceChildren();
      const title = titleInput.value.trim();
      const artistName = artistInput.value.trim();
      const musicUrl = urlInput.value.trim();
      if (!title || !artistName || !musicUrl) {
        resultLine.append(el("span", { class: "dim" },
          "title, artist and listening link are required"));
        return;
      }
      submit.disabled = true;
      submit.textContent = "saving…";
      try {
        const data = await api.createRelease({
          title,
          artist_name: artistName,
          music_url: musicUrl,
          genre: genreInput.value.trim(),
          storyline: storyInput.value.trim(),
        });
        resultLine.append(
          el("strong", {}, `added ${releaseTitle(data)} `),
          el("span", { class: "dim" }, releaseArtist(data)));
        form.reset();
        if (typeof onCreated === "function") onCreated();
      } catch (error) {
        resultLine.append(errorBanner(error));
      } finally {
        submit.disabled = false;
        submit.textContent = "Add release";
      }
    },
  },
    el("label", { class: "field" }, el("span", {}, "Title *"), titleInput),
    el("label", { class: "field" }, el("span", {}, "Artist *"), artistInput),
    el("label", { class: "field" }, el("span", {}, "Listening link *"),
      urlInput),
    el("label", { class: "field" }, el("span", {}, "Genre"), genreInput),
    el("label", { class: "field" }, el("span", {}, "Storyline"), storyInput),
    submit);

  return el("section", { class: "card" },
    el("h2", {}, "Add a music release"),
    el("p", { class: "dim" },
      "Record a release by its details and listening link. It is saved to "
      + "your library immediately and reused in outreach — no file upload "
      + "needed."),
    form,
    resultLine);
}

function trackDetailPanel(data, onSaved) {
  const record = data && typeof data === "object" ? data : {};
  const title = releaseTitle(record);
  const artist = releaseArtist(record);
  const uploadedAt = record.created_at || null;

  function kvRows(pairs) {
    const rows = pairs
      .filter(([, value]) => value !== null && value !== undefined
        && value !== "" && value !== "—" && value !== "–")
      .map(([key, value]) =>
        [el("dt", {}, key),
          el("dd", { class: key === "track_id" || key === "sha256" ? "track-id" : "" },
            String(value))]);
    return Array.isArray(rows) ? rows.flat() : [];
  }

  const main = el("div", { class: "track-overview" },
    el("h3", {}, title),
    artist ? el("p", {}, artist) : null,
    el("p", { class: "dim" }, "Release status · ",
      statusChip(record.status),
      uploadedAt ? el("span", { class: "dim" }, ` · added ${uploadedAt}`) : null));

  const techPairs = [
    ["asset id", record.track_id],
    ["sha256", record.sha256],
    ["title", record.title],
    ["artist", record.artist_name],
    ["genre", record.genre],
    ["file size (bytes)", record.size_bytes],
    ["content type", record.content_type],
    ["status", record.status],
    ["reject reason", record.reject_reason],
    ["notes", record.notes],
    ["listening url", record.listen_url],
    ["changed", record.updated_at],
    ["asset link", record.links && record.links.self],
  ];

  const tech = el("details", { class: "track-tech" },
    el("summary", {}, "Technical Details"),
    el("dl", { class: "kv" }, kvRows(techPairs)));

  return el("div", { class: "track-detail" },
    main,
    releaseMetadataEditor(record, onSaved),
    listenUrlEditor(record),
    songStoryEditor(record),
    tech);
}

function trackRow(track, detailBox, reload) {
  let detailsShown = false;
  const detailsButton = el("button", { class: "linkish" }, "Edit");
  detailsButton.addEventListener("click", async () => {
    if (detailsShown) {
      detailsShown = false;
      detailsButton.textContent = "Edit";
      detailBox.replaceChildren();
      return;
    }
    detailsButton.disabled = true;
    detailBox.replaceChildren(el("p", { class: "dim" }, "Loading…"));
    try {
      const data = await api.trackDetail(track.links.self);
      detailsShown = true;
      detailsButton.textContent = "Close";
      detailBox.replaceChildren(trackDetailPanel(data, reload));
    } catch (error) {
      detailBox.replaceChildren(error instanceof ApiError
        ? errorBanner(error)
        : el("p", { class: "banner-error" }, String(error)));
    } finally {
      detailsButton.disabled = false;
    }
  });

  const remove = el("button", { class: "subtle" }, "Remove");
  remove.addEventListener("click", () => {
    if (remove.disabled) return;
    const userConfirmed = window.confirm(
      `Remove release "${releaseTitle(track)}" from your library? Only the `
      + "stored record is deleted; any uploaded file bytes are untouched.");
    if (!userConfirmed) return;
    remove.disabled = true;
    remove.textContent = "removing…";
    api.deleteTrack(track.track_id)
      .then(() => {
        detailBox.replaceChildren();
        if (typeof reload === "function") reload();
      })
      .catch((error) => {
        remove.disabled = false;
        remove.textContent = "Remove";
        const message = error instanceof ApiError ? error.message : String(error);
        remove.after(el("span", { class: "dim", role: "alert" },
          `Could not remove: ${message}`));
      });
  });

  return el("tr", { class: "station-row" },
    el("td", {},
      el("div", {}, releaseTitle(track)),
      releaseArtist(track)
        ? el("div", { class: "dim" }, releaseArtist(track))
        : null,
      el("div", { class: "track-id" }, track.track_id)),
    el("td", {},
      String(track.listen_url || "").trim()
        ? listenLink(track.listen_url)
        : el("span", { class: "dim" }, "add a listening link")),
    el("td", { class: "dim" }, storylinePreview(track)),
    el("td", {},
      statusChip(track.status),
      track.reject_reason
        ? el("div", { class: "dim" }, track.reject_reason)
        : null),
    el("td", { class: "dim" }, String(track.created_at ?? "—")),
    el("td", { class: "actions-cell" }, detailsButton, " ", remove));
}

function summaryLine(total, limit, offset) {
  const from = total === 0 ? 0 : offset + 1;
  const to = Math.min(offset + limit, total);
  return `showing ${from}–${to} of ${total} releases`;
}

export function renderTracksView(root) {
  let state = { limit: 50, offset: 0 };
  const resultsCard = el("section", { class: "card" });
  const detailBox = el("div", {});

  const statusSelect = el(
    "select", { name: "status" },
    el("option", { value: "" }, "any status"),
    TRACK_STATUSES.map((value) =>
      el("option", { value, selected: state.status === value }, value)));
  const limitSelect = el(
    "select", { name: "limit" },
    LIMIT_CHOICES.map((value) =>
      el("option", { value, selected: Number(state.limit) === value },
        `${value} / page`)));

  const filterForm = el(
    "form",
    { class: "filter-form", onSubmit: (event) => {
      event.preventDefault();
      const applied = Object.fromEntries(
        new FormData(filterForm).entries());
      state.offset = 0;
      if (applied.status) state.status = applied.status;
      else delete state.status;
      if (applied.limit) state.limit = Number(applied.limit);
      load();
    } },
    el("label", { class: "field" }, el("span", {}, "status"), statusSelect),
    el("label", { class: "field" }, el("span", {}, "page size"),
      limitSelect),
    el("button", { class: "primary", type: "submit" }, "Apply filters"),
    el("button", {
      class: "subtle", type: "button",
      onClick: () => {
        state = { limit: 50, offset: 0 };
        load();
      },
    }, "Reset"),
  );

  function pagination(data) {
    const back = el("button", {
      disabled: data.offset === 0,
      onClick: () => {
        state.offset = Math.max(0, state.offset - data.limit);
        load();
      },
    }, "← previous");
    const next = el("button", {
      disabled: data.offset + data.limit >= data.total,
      onClick: () => { state.offset += data.limit; load(); },
    }, "next →");
    return el("div", { class: "actions-row" }, back, next);
  }

  async function load() {
    detailBox.replaceChildren();
    resultsCard.replaceChildren(el("p", { class: "dim" }, "Loading…"));
    try {
      const data = await api.tracks(state);
      resultsCard.replaceChildren(
        el("h2", {}, "Your releases"),
        el("p", { class: "dim" },
          summaryLine(data.total, data.limit, data.offset)),
        data.total === 0
          ? el("p", { class: "dim" },
            "No releases yet. Add one above by its title, artist and "
            + "listening link.")
          : el("table", { class: "results" },
            el("thead", {}, el("tr", {},
              el("th", {}, "release"), el("th", {}, "listening link"),
              el("th", {}, "storyline"), el("th", {}, "status"),
              el("th", {}, "added"), el("th", {}, "actions"))),
            el("tbody", {},
              (data.tracks || []).map((track) =>
                trackRow(track, detailBox, load)))),
        pagination(data));
    } catch (error) {
      resultsCard.replaceChildren(
        el("h2", {}, "Your releases"),
        errorBanner(error));
    }
  }

  root.append(
    releaseForm(load),
    el("section", { class: "card" },
      el("h2", {}, "Filter releases"),
      filterForm),
    resultsCard,
    detailBox);
  load();
}
