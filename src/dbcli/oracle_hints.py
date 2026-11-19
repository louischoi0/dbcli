"""Oracle optimizer hints, offered by the completion inside `/*+ ... */` (and anywhere else)."""

HINTS = (
    # statistics and plan display
    "gather_plan_statistics", "monitor", "no_monitor",
    # access paths
    "full", "index", "index_asc", "index_desc", "index_ffs", "index_ss", "index_combine",
    "index_join", "no_index", "no_index_ffs", "no_index_ss", "cluster", "hash",
    # join order and methods
    "leading", "ordered", "use_nl", "use_hash", "use_merge", "no_use_nl", "no_use_hash",
    "no_use_merge", "use_nl_with_index", "swap_join_inputs", "no_swap_join_inputs",
    # query transformations
    "unnest", "no_unnest", "merge", "no_merge", "push_pred", "no_push_pred", "push_subq",
    "no_push_subq", "use_concat", "no_expand", "star_transformation", "no_query_transformation",
    "materialize", "inline", "qb_name",
    # optimizer goals and features
    "all_rows", "first_rows", "optimizer_features_enable", "opt_param", "cardinality",
    "dynamic_sampling", "result_cache", "no_result_cache",
    # parallel and DML
    "parallel", "no_parallel", "pq_distribute", "append", "append_values", "noappend",
    "enable_parallel_dml", "disable_parallel_dml",
    # cursor sharing and binds
    "bind_aware", "no_bind_aware", "cursor_sharing_exact",
    # misc
    "driving_site", "rule", "ignore_row_on_dupkey_index", "change_dupkey_error_index",
    # the rest of the documented hints, and common undocumented ones
    "index_rs_asc", "index_rs_desc", "index_ss_asc", "index_ss_desc", "parallel_index",
    "no_parallel_index", "use_band", "no_use_band", "use_cube", "no_use_cube",
    "native_full_outer_join", "no_native_full_outer_join", "eliminate_join", "no_eliminate_join",
    "or_expand", "no_or_expand", "no_star_transformation", "fact", "no_fact", "rewrite",
    "no_rewrite", "fresh_mv", "place_group_by", "no_place_group_by", "use_hash_aggregation",
    "no_use_hash_aggregation", "gather_optimizer_statistics", "no_gather_optimizer_statistics",
    "adaptive_plan", "no_adaptive_plan", "pq_filter", "pq_skew", "no_pq_skew",
    "pq_concurrent_union", "no_pq_concurrent_union", "px_join_filter", "no_px_join_filter",
    "statement_queuing", "no_statement_queuing", "cache", "nocache", "inmemory", "no_inmemory",
    "inmemory_pruning", "no_inmemory_pruning", "clustering", "no_clustering", "no_zonemap",
    "containers", "grouping", "model_min_analysis", "retry_on_row_change",
    "no_xml_query_rewrite", "no_xmlindex_rewrite", "use_invisible_indexes",
    "no_use_invisible_indexes", "outline_leaf", "ignore_optim_embedded_hints", "opt_estimate",
    "no_cpu_costing", "precompute_subquery", "set_to_join", "no_set_to_join",
    "full_outer_join_to_outer", "no_full_outer_join_to_outer", "no_elim_groupby", "elim_groupby",
)

HINT_WEIGHT = 3  # below everyday keywords, above words never seen
