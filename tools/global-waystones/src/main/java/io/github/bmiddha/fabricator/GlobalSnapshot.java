package io.github.bmiddha.fabricator;

import java.util.Collection;
import java.util.LinkedHashMap;
import java.util.Map;

/** Replacement snapshots make privacy transitions and deleted dimensions omissions. */
final class GlobalSnapshot {
    record Stone(String id, String dimension, String name, boolean global,
                 boolean regular, boolean valid, boolean transientStone, int x, int y, int z) {}

    static Map<String, Map<String, Stone>> reconcile(Collection<Stone> stones) {
        Map<String, Map<String, Stone>> result = new LinkedHashMap<>();
        for (Stone stone : stones) {
            if (!stone.global() || !stone.regular() || !stone.valid() || stone.transientStone()) continue;
            result.computeIfAbsent(stone.dimension(), ignored -> new LinkedHashMap<>())
                    .put(stone.id(), stone);
        }
        return result;
    }

    static String escape(String text) {
        return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                .replace("\"", "&quot;").replace("'", "&#39;");
    }
}
