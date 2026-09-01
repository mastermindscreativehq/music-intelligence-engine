/* Deterministic outreach draft builder (Part 4).
 *
 * Assembles a greeting, recommended subject, and message from VERIFIED
 * fields ONLY — recipient (name/role/organization/evidence), the selected
 * track, and optional artist context. It never invents a relationship
 * ("I've been listening to your show" is NEVER added), never fabricates
 * facts, and omits anything that is unknown. Re-generating with the same
 * inputs yields the same output, so it is testable and predictable.
 */

function clean(value) {
  return typeof value === "string" ? value.trim() : "";
}

function humanRole(role) {
  return clean(role).replace(/_/g, " ") || "music team";
}

export function generateDraft(recipient, track, artist) {
  const rec = recipient && typeof recipient === "object" ? recipient : {};
  const trk = track && typeof track === "object" ? track : {};
  const art = artist && typeof artist === "object" ? artist : {};

  const recipientName = clean(rec.name);
  const role = humanRole(rec.role);
  const org = clean(rec.organization) || clean(rec.station_name);
  const trackTitle = clean(trk.title) || clean(trk.original_filename);
  const artistName = clean(art.name);
  const listenUrl = clean(trk.listen_url);
  const description = clean(trk.description) || clean(art.bio);

  // ---- Greeting ------------------------------------------------------------
  const greeting = recipientName ? `Hello ${recipientName},` : "Hello,";

  // ---- Subject ---------------------------------------------------------------
  let subject;
  if (trackTitle && artistName) subject = `${artistName} — "${trackTitle}"`;
  else if (trackTitle) subject = `"${trackTitle}"`;
  else if (artistName) subject = `${artistName}`;
  else subject = "New music submission";
  if (org) subject += ` for ${org}`;

  // ---- Message body -----------------------------------------------------------
  const lines = [greeting, ""];

  if (org) {
    lines.push(`I'm writing to the ${role} at ${org} to share a piece of ` +
      "music with the hope it fits what you feature.");
  } else {
    lines.push("I'm writing to share a piece of music with the hope it " +
      "fits what you feature.");
  }
  lines.push("");

  const whats = [];
  if (trackTitle) whats.push(`the track "${trackTitle}"`);
  if (artistName && !whats.length) whats.push(`${artistName}'s music`);
  if (!whats.length) whats.push("a new track");
  lines.push(`I'd love for you to hear ${whats.join(" and ")} and consider ` +
    "it for airplay.");
  lines.push("");

  if (listenUrl) {
    lines.push("You can listen here:");
    lines.push(listenUrl);
    lines.push("");
  }

  if (description) {
    lines.push(description.length > 260 ? description.slice(0, 260) + "…"
      : description);
    lines.push("");
  }

  lines.push("Thank you for your time and for all you do for music.");

  if (artistName) {
    lines.push("");
    lines.push("Warm regards,");
    lines.push(artistName);
  }

  return {
    greeting,
    subject,
    message: lines.join("\n"),
  };
}

export const DRAFT_GEN_VERSION = 1;
