package com.smartinspector.hook.repository

import com.smartinspector.hook.model.Item
import org.json.JSONArray
import org.json.JSONObject


class DataRepository {


    fun loadItemsSync(count: Int): List<Item> {
        // P0: Simulate expensive synchronous I/O
        Thread.sleep(50)

        val items = mutableListOf<Item>()
        for (i in 0 until count) {
            items.add(
                Item(
                    title = "Item #$i",
                    index = i,
                    category = categories[i % categories.size],
                    payload = buildPayload(i)
                )
            )
        }
        return items
    }


    fun loadItemsJson(count: Int): List<Item> {
        // Build a large JSON array
        val arr = JSONArray()
        for (i in 0 until count) {
            val obj = JSONObject()
            obj.put("title", "Item #$i")
            obj.put("index", i)
            obj.put("category", categories[i % categories.size])
            obj.put("payload", buildPayload(i))
            obj.put("extra1", "padding-$i")
            obj.put("extra2", "data-$i")
            obj.put("extra3", "filler-$i")
            arr.put(obj)
        }

        // P1: Parsing this large JSON on main thread is expensive
        val result = mutableListOf<Item>()
        for (i in 0 until arr.length()) {
            val obj = arr.getJSONObject(i)
            result.add(
                Item(
                    title = obj.getString("title"),
                    index = obj.getInt("index"),
                    category = obj.getString("category"),
                    payload = obj.getString("payload")
                )
            )
        }
        return result
    }

    private fun buildPayload(seed: Int): String {
        var result = ""
        for (i in 0 until 100) {
            result += "token-$seed-$i "
        }
        return result.trim()
    }

    companion object {
        private val categories = listOf(
            "Featured", "Popular", "New", "Recommended", "Trending"
        )
    }
}
