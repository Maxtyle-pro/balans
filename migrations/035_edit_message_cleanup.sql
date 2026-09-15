SET search_path=balans,pg_catalog;

-- Telegram message IDs are used only to clean up the transient edit flow.
-- Financial history remains immutable and is not deleted by this UI cleanup.
ALTER TABLE operation_drafts
  ADD COLUMN edit_origin_message_id bigint,
  ADD COLUMN edit_message_ids bigint[] NOT NULL DEFAULT '{}';
