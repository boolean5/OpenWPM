type DateTime = string;

export interface HttpRequest {
  id?: number;
  crawl_id?: number;
  visit_id?: number;
  url: string;
  top_level_url?: string;
  method: string;
  referrer: string;
  headers: string;
  channel_id: string;
  is_XHR?: number;
  is_frame_load?: number;
  is_full_page?: number;
  is_third_party_channel?: number;
  is_third_party_to_top_window?: number;
  triggering_origin?: string;
  loading_origin?: string;
  loading_href?: string;
  req_call_stack?: string;
  resource_type: string;
  post_body?: string;
  time_stamp: string;
}

export interface HttpResponse {
  id?: number;
  crawl_id?: number;
  visit_id?: number;
  url: string;
  method: string;
  referrer: string;
  response_status: number;
  response_status_text: string;
  is_cached: number;
  headers: string;
  channel_id: string;
  location: string;
  time_stamp: string;
  content_hash?: string;
}

export interface HttpRedirect {
  id?: number;
  crawl_id?: number;
  visit_id?: number;
  old_channel_id?: string;
  new_channel_id?: string;
  is_temporary: number;
  is_permanent: number;
  is_internal: number;
  is_sts_upgrade: number;
  time_stamp: string;
}

export interface JavascriptOperation {
  id?: number;
  crawl_id?: number;
  visit_id?: number;
  script_url?: string;
  script_line?: string;
  script_col?: string;
  func_name?: string;
  script_loc_eval?: string;
  document_url?: string;
  top_level_url?: string;
  call_stack?: string;
  symbol?: string;
  operation?: string;
  value?: string;
  arguments?: string;
  time_stamp: string;
}

export interface JavascriptCookieChange {
  id?: number;
  crawl_id?: number;
  visit_id?: number;
  change?: "deleted" | "added" | "changed";
  creationTime?: DateTime;
  expiry?: DateTime;
  is_http_only?: number;
  is_host_only?: number;
  is_session?: number;
  last_accessed?: DateTime;
  raw_host?: string;
  expires?: number;
  host?: string;
  is_domain?: number;
  is_secure?: number;
  name?: string;
  path?: string;
  policy?: number;
  status?: number;
  value?: string;
  same_site?: string;
  first_party_domain?: string;
}
