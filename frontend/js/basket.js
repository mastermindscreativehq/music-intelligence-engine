/* Recipient selection (Phase 7 scope: SELECT only — nothing is sent).
 *
 * Recipients are individual contacts identified by the backend-stable
 * contact_uid. The UI never computes who is "preferred"; that business
 * rule lives in the backend (preferred_for_submissions /
 * preferred_submission_contacts). Selection is an operator decision
 * recorded on top of backend output.
 *
 * Persisted in sessionStorage so a refresh keeps the working set without
 * ever writing to a server or sending anything.
 */

const STORAGE_KEY = "mie.recipients.v1";

export class Basket {
  constructor(storage) {
    this.storage = storage;
    this.items = this._load();
    this.listeners = new Set();
  }

  _load() {
    try {
      const parsed = JSON.parse(this.storage.getItem(STORAGE_KEY));
      if (!Array.isArray(parsed)) return [];
      return parsed.filter(
        (item) =>
          item &&
          typeof item.contact_uid === "string" &&
          typeof item.identity_key === "string",
      );
    } catch (error) {
      return [];
    }
  }

  _save() {
    try {
      this.storage.setItem(STORAGE_KEY, JSON.stringify(this.items));
    } catch (error) {
      /* storage full/unavailable: keep in-memory selection */
    }
    this._emit();
  }

  subscribe(listener) {
    this.listeners.add(listener);
    listener(this.items);
    return () => this.listeners.delete(listener);
  }

  _emit() {
    for (const listener of this.listeners) listener(this.items);
  }

  has(contactUid) {
    return this.items.some((item) => item.contact_uid === contactUid);
  }

  get(contactUid) {
    const uid = String(contactUid);
    return this.items.find((item) => item.contact_uid === uid) || null;
  }

  add(recipient) {
    if (!recipient || !recipient.contact_uid || this.has(recipient.contact_uid)) {
      return false;
    }
    this.items.push({
      contact_uid: String(recipient.contact_uid),
      identity_key: String(recipient.identity_key),
      station_name: recipient.station_name ?? null,
      name: recipient.name ?? null,
      role: recipient.role ?? null,
      email: recipient.email ?? null,
      source_url: recipient.source_url ?? null,
    });
    this._save();
    return true;
  }

  remove(contactUid) {
    this.items = this.items.filter((item) => item.contact_uid !== contactUid);
    this._save();
  }

  clear() {
    this.items = [];
    this._save();
  }

  exportPayload() {
    return {
      generated_at: new Date().toISOString(),
      note:
        "Operator-selected outreach recipients. Pre-outreach export only; " +
        "this application never sends messages.",
      recipients: this.items.map((item) => ({ ...item })),
    };
  }
}
