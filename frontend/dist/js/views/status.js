/* Plain-language status chips shared across views.
 *
 * Every chip maps a backend-derived status to a short human label plus an
 * existing chip color class. The backend owns interpretation; this module is
 * presentation only and never guesses a status the backend did not send. */

import { el } from "../dom.js";

function chip(label, cls) {
  return el("span", { class: `chip ${cls}` }, label);
}

/* DJ classification (detail.classification_status from the discovery gate):
 *   verified           -> "Verified DJ"
 *   needs_verification -> "DJ"        (recorded, evidence not yet confirmed)
 *   not_qualified      -> "Not a DJ"  (evidence says this record is NOT a DJ;
 *                                       hidden from the normal listing) */
const DJ_STATUS_CHIP = {
  verified: ["Verified DJ", "status-verified"],
  needs_verification: ["DJ", "status-unverified"],
  not_qualified: ["Not a DJ", "status-stale"],
};

export function djStatusChip(dj) {
  const entry = DJ_STATUS_CHIP[dj && dj.classification_status]
    || DJ_STATUS_CHIP.needs_verification;
  return chip(entry[0], entry[1]);
}

/* Station research (detail.research_status, derived from stored evidence):
 *   verified / partially_researched / needs_research */
const RESEARCH_STATUS_CHIP = {
  verified: ["Verified", "status-verified"],
  partially_researched: ["Partially researched", "status-enriched"],
  needs_research: ["Needs research", "status-unverified"],
};

export function stationResearchChip(status) {
  const entry = RESEARCH_STATUS_CHIP[status]
    || RESEARCH_STATUS_CHIP.needs_research;
  return chip(entry[0], entry[1]);
}

/* Submission route (intel.submission_status): a truthful statement about the
 * station-published routes on record. */
const SUBMISSION_STATUS_CHIP = {
  DIRECT_SUBMISSION: ["Music submission available", "status-verified"],
  CONTACT_FOR_SUBMISSION: ["Contact route available", "status-enriched"],
  SHOW_SPECIFIC_OPPORTUNITY: ["Show-specific opportunity", "status-new"],
  NO_PUBLIC_SUBMISSION_ROUTE_FOUND: [
    "No public submission route found", "status-stale"],
};

export function submissionStatusChip(status) {
  const entry = SUBMISSION_STATUS_CHIP[status]
    || ["Not yet researched", "status-unverified"];
  return chip(entry[0], entry[1]);
}

/* Contact availability: a verified email decision-maker takes priority over
 * an official contact page, and both over "none found". */
export function contactStatusChip(emailContact, contactPage) {
  if (emailContact) return chip("Verified contact", "status-verified");
  if (contactPage) return chip("Contact found", "status-enriched");
  return chip("No contact found", "status-stale");
}