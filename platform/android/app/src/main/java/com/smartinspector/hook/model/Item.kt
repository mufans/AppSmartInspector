package com.smartinspector.hook.model

/** Data model for RecyclerView items. */
data class Item(
    val title: String,
    val index: Int,
    val category: String,
    val payload: String = ""
)
