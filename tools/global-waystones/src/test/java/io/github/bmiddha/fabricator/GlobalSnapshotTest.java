package io.github.bmiddha.fabricator;

import org.junit.jupiter.api.Test;
import de.bluecolored.bluemap.api.gson.MarkerGson;
import de.bluecolored.bluemap.api.markers.MarkerSet;
import de.bluecolored.bluemap.api.markers.POIMarker;

import java.nio.file.Files;
import java.nio.file.Path;
import java.util.List;
import java.util.Map;

import static org.junit.jupiter.api.Assertions.*;

class GlobalSnapshotTest {
    private GlobalSnapshot.Stone stone(String id, String dimension, String name, boolean global) {
        return new GlobalSnapshot.Stone(id, dimension, name, global, true, true, false, 1, 64, 2);
    }

    @Test void onlyExplicitGlobalsAreIncluded() {
        var result = GlobalSnapshot.reconcile(List.of(
                stone("global", "minecraft:overworld", "Town", true),
                stone("private", "minecraft:overworld", "Activation only", false),
                stone("team", "minecraft:overworld", "Team", false),
                new GlobalSnapshot.Stone("home", "minecraft:overworld", "Home", true,
                        false, true, false, 0, 0, 0),
                new GlobalSnapshot.Stone("death", "minecraft:overworld", "Death", true,
                        true, true, true, 0, 0, 0),
                new GlobalSnapshot.Stone("invalid", "minecraft:overworld", "Invalid", true,
                        true, false, false, 0, 0, 0)));
        assertEquals(List.of("global"), List.copyOf(result.get("minecraft:overworld").keySet()));
    }

    @Test void renameDimensionMoveAndRestartReplaceEntireSnapshot() {
        var original = GlobalSnapshot.reconcile(List.of(stone("uuid", "minecraft:overworld", "Before", true)));
        var moved = GlobalSnapshot.reconcile(List.of(stone("uuid", "cobbleverse:exact_dimension", "After", true)));
        assertFalse(moved.containsKey("minecraft:overworld"));
        assertEquals("After", moved.get("cobbleverse:exact_dimension").get("uuid").name());
        assertEquals("Before", original.get("minecraft:overworld").get("uuid").name());
        assertTrue(GlobalSnapshot.reconcile(List.of(stone("uuid", "minecraft:overworld", "Private", false))).isEmpty());
        assertTrue(GlobalSnapshot.reconcile(List.of()).isEmpty());
    }

    @Test void realBlueMapSerializationPreservesStableIdsAndEscapedLabels() throws Exception {
        var set = MarkerSet.builder().label("Global waystones").build();
        set.put("stable-id", POIMarker.builder().label("Synthetic town <global>")
                .detail(GlobalSnapshot.escape("Synthetic town <global>"))
                .position(0.5, 64, 0.5).build());
        var json = MarkerGson.INSTANCE.toJsonTree(Map.of("fabricator:global-waystones", set));
        var marker = json.getAsJsonObject().getAsJsonObject("fabricator:global-waystones")
                .getAsJsonObject("markers").getAsJsonObject("stable-id");
        assertEquals("poi", marker.get("type").getAsString());
        assertEquals("Synthetic town &lt;global&gt;", marker.get("label").getAsString());
        assertEquals("Synthetic town &lt;global&gt;", marker.get("detail").getAsString());
        assertEquals(0.5, marker.getAsJsonObject("position").get("x").getAsDouble());
        Files.writeString(Path.of("build/test-markers.json"), json.toString());
    }

    @Test void markerDetailsCannotCreateHtml() {
        assertEquals("&lt;img src=x onerror=&#39;alert(1)&#39;&gt;&amp;&quot;",
                GlobalSnapshot.escape("<img src=x onerror='alert(1)'>&\""));
    }
}
