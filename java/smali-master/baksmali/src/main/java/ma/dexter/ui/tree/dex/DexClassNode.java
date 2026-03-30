// app/src/main/java/ma/dexter/ui/tree/dex/DexClassNode.java
package ma.dexter.ui.tree.dex;

import java.util.Objects;

/**
 * Base class to represent nodes in a Dex tree.
 *
 * For example, name could be "android" in "android/app/Activity"
 * (or "android.app" if compactMiddlePackages was run.)
 */
public class DexClassNode {
    private String name;

    public DexClassNode(String name) {
        this.name = name;
    }

    public String getName() {
        return name;
    }

    public void setName(String name) {
        this.name = name;
    }

    @Override
    public boolean equals(Object other) {
        if (this == other) return true;
        if (other == null || getClass() != other.getClass()) return false;

        DexClassNode that = (DexClassNode) other;
        return Objects.equals(name, that.name);
    }

    @Override
    public int hashCode() {
        return Objects.hash(name);
    }

    @Override
    public String toString() {
        return name;
    }
}