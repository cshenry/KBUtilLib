# Escher Utils Refactoring Request

## Original User Request

I want to refactor the escher-utils module. It's currently too complicated with too many utility and sub-functions. I want to simplify it and I want to take a different approach. The current approach does not yield working code. The problem we presently have with the Escher maps is that the maps do not display reverse direction reactions correctly.

To deal with this problem, I want to take a new approach, which is to modify the map to fit the provided flux solution. When a map is fed into the create_map_html function, the code should examine the flux solution and reverse the directions by multiplying all metabolite coefficients by -1 of all reactions that have a negative flux and then change the flux from negative to positive. In this way, the map will have been adapted to fit the flux solution. Ideally, this transformation of the map should happen in memory because the base map should never be modified. At this point, the build function should be called to render the map in HTML.

I'd like to keep all the functionality around displaying proteomes and metabolomes. But again, I would like to see this simplified as much as possible. I would also like to keep functionality around flux thresholds to eliminate very small fluxes, and ideally keep the functionality around custom coloring of fluxes. And again, I don't want to do something that modifies the core map behavior, because this seems to be leading to the maps not working right. I still want to be able to use the Escher functionality like adding reactions to the map.

To ensure that reaction addition functionality works properly, we should consider an optional feature that would also modify the reactions in the model to change their directionalities according to the flux solution. In fact, I think this is an essential feature because if we don't do this, the model directionalities will not match the map directionalities, and this will cause problems.
